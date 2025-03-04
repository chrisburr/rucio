# -*- coding: utf-8 -*-
# Copyright 2013-2021 CERN
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
# - Mario Lassnig <mario.lassnig@cern.ch>, 2013-2021
# - Martin Barisits <martin.barisits@cern.ch>, 2017-2021
# - Vincent Garonne <vincent.garonne@cern.ch>, 2017
# - Igor Mandrichenko <rucio@fermicloud055.fnal.gov>, 2018
# - Cedric Serfon <cedric.serfon@cern.ch>, 2018-2021
# - dciangot <diego.ciangottini@cern.ch>, 2018
# - Robert Illingworth <illingwo@fnal.gov>, 2018-2019
# - Hannes Hansen <hannes.jakob.hansen@cern.ch>, 2018
# - Andrew Lister <andrew.lister@stfc.ac.uk>, 2019
# - Brandon White <bjwhite@fnal.gov>, 2019
# - Matt Snyder <msnyder@bnl.gov>, 2019-2021
# - Gabriele Fronze' <gfronze@cern.ch>, 2019
# - Jaroslav Guenther <jaroslav.guenther@cern.ch>, 2019-2020
# - Eric Vaandering <ewv@fnal.gov>, 2020
# - Eli Chadwick <eli.chadwick@stfc.ac.uk>, 2020
# - Nick Smith <nick.smith@cern.ch>, 2020
# - Patrick Austin <patrick.austin@stfc.ac.uk>, 2020
# - Benedikt Ziemons <benedikt.ziemons@cern.ch>, 2020
# - Thomas Beermann <thomas.beermann@cern.ch>, 2021
# - Radu Carpa <radu.carpa@cern.ch>, 2021

from __future__ import division

import copy
import datetime
import json
import logging
import re
import sys
import time
from typing import TYPE_CHECKING

from dogpile.cache import make_region
from dogpile.cache.api import NoValue
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.expression import false

from rucio.common import constants
from rucio.common.config import config_get
from rucio.common.constants import SUPPORTED_PROTOCOLS, FTS_STATE
from rucio.common.exception import (InvalidRSEExpression, NoDistance,
                                    RequestNotFound, RSEProtocolNotSupported,
                                    RucioException, UnsupportedOperation)
from rucio.common.rse_attributes import get_rse_attributes
from rucio.common.types import InternalAccount
from rucio.common.utils import construct_surl
from rucio.core import did, message as message_core, request as request_core
from rucio.core.config import get as core_config_get
from rucio.core.monitor import record_counter, record_timer
from rucio.core.oidc import get_token_for_account_operation
from rucio.core.replica import add_replicas
from rucio.core.request import queue_requests, set_requests_state
from rucio.core.rse import get_rse_name, get_rse_vo, list_rses, get_rse_supported_checksums
from rucio.core.rse_expression_parser import parse_expression
from rucio.db.sqla import models, filter_thread_work
from rucio.db.sqla.constants import DIDType, RequestState, RSEType, RequestType, ReplicaState
from rucio.db.sqla.session import read_session, transactional_session
from rucio.rse import rsemanager as rsemgr
from rucio.transfertool.fts3 import FTS3Transfertool
from rucio.transfertool.mock import MockTransfertool

if TYPE_CHECKING:
    from typing import List, Tuple

# Extra modules: Only imported if available
EXTRA_MODULES = {'globus_sdk': False}

for extra_module in EXTRA_MODULES:
    if sys.version_info < (3, 5):
        try:
            import imp
            imp.find_module(extra_module)
            EXTRA_MODULES[extra_module] = True
        except ImportError:
            EXTRA_MODULES[extra_module] = False

    else:
        try:
            import importlib
            importlib.util.find_spec(extra_module)
            EXTRA_MODULES[extra_module] = True
        except ModuleNotFoundError:
            EXTRA_MODULES[extra_module] = False

if EXTRA_MODULES['globus_sdk']:
    from rucio.transfertool.globus import GlobusTransferTool  # pylint: disable=import-error


"""
The core transfer.py is specifically for handling transfer-requests, thus requests
where the external_id is already known.
Requests accessed by request_id  are covered in the core request.py
"""

REGION_SHORT = make_region().configure('dogpile.cache.memcached',
                                       expiration_time=600,
                                       arguments={'url': config_get('cache', 'url', False, '127.0.0.1:11211'), 'distributed_lock': True})
ALLOW_USER_OIDC_TOKENS = config_get('conveyor', 'allow_user_oidc_tokens', False, False)
REQUEST_OIDC_SCOPE = config_get('conveyor', 'request_oidc_scope', False, 'fts:submit-transfer')
REQUEST_OIDC_AUDIENCE = config_get('conveyor', 'request_oidc_audience', False, 'fts:example')

WEBDAV_TRANSFER_MODE = config_get('conveyor', 'webdav_transfer_mode', False, None)


def submit_bulk_transfers(external_host, files, transfertool='fts3', job_params={}, timeout=None, user_transfer_job=False, logger=logging.log):
    """
    Submit transfer request to a transfertool.
    :param external_host:  External host name as string
    :param files:          List of Dictionary containing request file.
    :param transfertool:   Transfertool as a string.
    :param job_params:     Metadata key/value pairs for all files as a dictionary.
    :param logger:         Optional decorated logger that can be passed from the calling daemons or servers.
    :returns:              Transfertool external ID.
    """

    record_counter('core.request.submit_transfer')

    transfer_id = None

    if transfertool == 'fts3':
        start_time = time.time()
        job_files = []
        for file in files:
            job_file = {}
            for key in file:
                if key == 'sources':
                    # convert sources from (src_rse, url, src_rse_id, rank) to url
                    job_file[key] = []
                    for source in file[key]:
                        job_file[key].append(source[1])
                else:
                    job_file[key] = file[key]
            job_files.append(job_file)

        # getting info about account and OIDC support of the RSEs
        use_oidc = job_params.get('use_oidc', False)
        transfer_token = None
        if use_oidc:
            logger(logging.DEBUG, 'OAuth2/OIDC available at RSEs')
            account = job_params.get('account', None)
            getadmintoken = False
            if ALLOW_USER_OIDC_TOKENS is False:
                getadmintoken = True
            logger(logging.DEBUG, 'Attempting to get a token for account %s. Admin token option set to %s' % (account, getadmintoken))
            # find the appropriate OIDC token and exchange it (for user accounts) if necessary
            token_dict = get_token_for_account_operation(account, req_audience=REQUEST_OIDC_AUDIENCE, req_scope=REQUEST_OIDC_SCOPE, admin=getadmintoken)
            if token_dict is not None:
                logger(logging.DEBUG, 'Access token has been granted.')
                if 'token' in token_dict:
                    logger(logging.DEBUG, 'Access token used as transfer token.')
                    transfer_token = token_dict['token']
        transfer_id = FTS3Transfertool(external_host=external_host, token=transfer_token).submit(files=job_files, job_params=job_params, timeout=timeout)
        record_timer('core.request.submit_transfers_fts3', (time.time() - start_time) * 1000 / len(files))
    elif transfertool == 'globus':
        logger(logging.DEBUG, '... Starting globus xfer ...')
        job_files = []
        for file in files:
            job_file = {}
            for key in file:
                if key == 'sources':
                    # convert sources from (src_rse, url, src_rse_id, rank) to url
                    job_file[key] = []
                    for source in file[key]:
                        job_file[key].append(source[1])
                else:
                    job_file[key] = file[key]
            job_files.append(job_file)
        logger(logging.DEBUG, 'job_files: %s' % job_files)
        transfer_id = GlobusTransferTool(external_host=None).bulk_submit(submitjob=job_files, timeout=timeout)
    elif transfertool == 'mock':
        transfer_id = MockTransfertool(external_host=None).submit(files, None)
    return transfer_id


@transactional_session
def prepare_sources_for_transfers(transfers, session=None):
    """
    Prepare the sources for transfers.
    :param transfers:  Dictionary containing request transfer info.
    :param session:    Database session to use.
    """

    try:
        for request_id in transfers:
            rowcount = session.query(models.Request)\
                              .filter_by(id=request_id)\
                              .filter(models.Request.state == RequestState.QUEUED)\
                              .update({'state': transfers[request_id]['state'],
                                       'external_id': transfers[request_id]['external_id'],
                                       'external_host': transfers[request_id]['external_host'],
                                       'dest_url': transfers[request_id]['dest_url'],
                                       'submitted_at': datetime.datetime.utcnow()},
                                      synchronize_session=False)
            if rowcount == 0:
                raise RequestNotFound("Failed to prepare transfer: request %s does not exist or is not in queued state" % (request_id))

            if 'file' in transfers[request_id]:
                file = transfers[request_id]['file']
                for src_rse, src_url, src_rse_id, rank in file['sources']:
                    src_rowcount = session.query(models.Source)\
                                          .filter_by(request_id=request_id)\
                                          .filter(models.Source.rse_id == src_rse_id)\
                                          .update({'is_using': True}, synchronize_session=False)
                    if src_rowcount == 0:
                        models.Source(request_id=file['metadata']['request_id'],
                                      scope=file['metadata']['scope'],
                                      name=file['metadata']['name'],
                                      rse_id=src_rse_id,
                                      dest_rse_id=file['metadata']['dest_rse_id'],
                                      ranking=rank if rank else 0,
                                      bytes=file['metadata']['filesize'],
                                      url=src_url,
                                      is_using=True).\
                            save(session=session, flush=False)

    except IntegrityError as error:
        raise RucioException(error.args)


@transactional_session
def set_transfers_state(transfers, submitted_at, session=None):
    """
    Update the transfer info of a request.
    :param transfers:  Dictionary containing request transfer info.
    :param session:    Database session to use.
    """

    try:
        for request_id in transfers:
            rowcount = session.query(models.Request)\
                              .filter_by(id=request_id)\
                              .filter(models.Request.state == RequestState.SUBMITTING)\
                              .update({'state': transfers[request_id]['state'],
                                       'external_id': transfers[request_id]['external_id'],
                                       'external_host': transfers[request_id]['external_host'],
                                       'source_rse_id': transfers[request_id]['src_rse_id'],
                                       'submitted_at': submitted_at},
                                      synchronize_session=False)
            if rowcount == 0:
                raise RucioException("Failed to set requests %s tansfer %s: request doesn't exist or is not in SUBMITTING state" % (request_id, transfers[request_id]))

            request_type = transfers[request_id].get('request_type', None)

            msg = {'request-id': request_id,
                   'request-type': request_type,
                   'scope': transfers[request_id]['scope'].external,
                   'name': transfers[request_id]['name'],
                   'src-rse-id': transfers[request_id]['metadata'].get('src_rse_id', None),
                   'src-rse': transfers[request_id]['metadata'].get('src_rse', None),
                   'dst-rse-id': transfers[request_id]['metadata'].get('dst_rse_id', None),
                   'dst-rse': transfers[request_id]['metadata'].get('dst_rse', None),
                   'state': transfers[request_id]['state'],
                   'activity': transfers[request_id]['metadata'].get('activity', None),
                   'file-size': transfers[request_id]['metadata'].get('filesize', None),
                   'bytes': transfers[request_id]['metadata'].get('filesize', None),
                   'checksum-md5': transfers[request_id]['metadata'].get('md5', None),
                   'checksum-adler': transfers[request_id]['metadata'].get('adler32', None),
                   'external-id': transfers[request_id]['external_id'],
                   'external-host': transfers[request_id]['external_host'],
                   'queued_at': str(submitted_at)}
            if transfers[request_id]['scope'].vo != 'def':
                msg['vo'] = transfers[request_id]['scope'].vo

            if msg['request-type']:
                transfer_status = '%s-%s' % (msg['request-type'].name, msg['state'].name)
            else:
                transfer_status = 'transfer-%s' % msg['state']
            transfer_status = transfer_status.lower()

            message_core.add_message(transfer_status, msg, session=session)

    except IntegrityError as error:
        raise RucioException(error.args)


def bulk_query_transfers(request_host, transfer_ids, transfertool='fts3', timeout=None, logger=logging.log):
    """
    Query the status of a transfer.
    :param request_host:  Name of the external host.
    :param transfer_ids:  List of (External-ID as a 32 character hex string)
    :param transfertool:  Transfertool name as a string.
    :param logger:        Optional decorated logger that can be passed from the calling daemons or servers.
    :returns:             Request status information as a dictionary.
    """

    record_counter('core.request.bulk_query_transfers')

    if transfertool == 'fts3':
        try:
            start_time = time.time()
            fts_resps = FTS3Transfertool(external_host=request_host).bulk_query(transfer_ids=transfer_ids, timeout=timeout)
            record_timer('core.request.bulk_query_transfers', (time.time() - start_time) * 1000 / len(transfer_ids))
        except Exception:
            raise

        for transfer_id in transfer_ids:
            if transfer_id not in fts_resps:
                fts_resps[transfer_id] = Exception("Transfer id %s is not returned" % transfer_id)
            if fts_resps[transfer_id] and not isinstance(fts_resps[transfer_id], Exception):
                for request_id in fts_resps[transfer_id]:
                    if fts_resps[transfer_id][request_id]['file_state'] in (FTS_STATE.FAILED,
                                                                            FTS_STATE.FINISHEDDIRTY,
                                                                            FTS_STATE.CANCELED):
                        fts_resps[transfer_id][request_id]['new_state'] = RequestState.FAILED
                    elif fts_resps[transfer_id][request_id]['file_state'] in FTS_STATE.FINISHED:
                        fts_resps[transfer_id][request_id]['new_state'] = RequestState.DONE
        return fts_resps
    elif transfertool == 'globus':
        try:
            start_time = time.time()
            logger(logging.DEBUG, 'transfer_ids: %s' % transfer_ids)
            responses = GlobusTransferTool(external_host=None).bulk_query(transfer_ids=transfer_ids, timeout=timeout)
            record_timer('core.request.bulk_query_transfers', (time.time() - start_time) * 1000 / len(transfer_ids))
        except Exception:
            raise

        for k, v in responses.items():
            if v == 'FAILED':
                responses[k] = RequestState.FAILED
            elif v == 'SUCCEEDED':
                responses[k] = RequestState.DONE
            else:
                responses[k] = RequestState.SUBMITTED
        return responses
    else:
        raise NotImplementedError

    return None


@transactional_session
def set_transfer_update_time(external_host, transfer_id, update_time=datetime.datetime.utcnow(), session=None):
    """
    Update the state of a request. Fails silently if the transfer_id does not exist.
    :param external_host:  Selected external host as string in format protocol://fqdn:port
    :param transfer_id:    External transfer job id as a string.
    :param update_time:    Time stamp.
    :param session:        Database session to use.
    """

    record_counter('core.request.set_transfer_update_time')

    try:
        rowcount = session.query(models.Request).filter_by(external_id=transfer_id, state=RequestState.SUBMITTED).update({'updated_at': update_time}, synchronize_session=False)
    except IntegrityError as error:
        raise RucioException(error.args)

    if not rowcount:
        raise UnsupportedOperation("Transfer %s doesn't exist or its status is not submitted." % (transfer_id))


def query_latest(external_host, state, last_nhours=1, logger=logging.log):
    """
    Query the latest transfers in last n hours with state.
    :param external_host:  FTS host name as a string.
    :param state:          FTS job state as a string or a dictionary.
    :param last_nhours:    Latest n hours as an integer.
    :param logger:         Optional decorated logger that can be passed from the calling daemons or servers.
    :returns:              Requests status information as a dictionary.
    """

    record_counter('core.request.query_latest')

    start_time = time.time()
    resps = FTS3Transfertool(external_host=external_host).query_latest(state=state, last_nhours=last_nhours)
    record_timer('core.request.query_latest_fts3.%s.%s_hours' % (external_host, last_nhours), (time.time() - start_time) * 1000)

    if not resps:
        return

    ret_resps = []
    for resp in resps:
        if 'job_metadata' not in resp or resp['job_metadata'] is None or 'issuer' not in resp['job_metadata'] or resp['job_metadata']['issuer'] != 'rucio':
            continue

        if 'request_id' not in resp['job_metadata']:
            # submitted by new submitter
            try:
                logger(logging.DEBUG, "Transfer %s on %s is %s, decrease its updated_at." % (resp['job_id'], external_host, resp['job_state']))
                set_transfer_update_time(external_host, resp['job_id'], datetime.datetime.utcnow() - datetime.timedelta(hours=24))
            except Exception as error:
                logger(logging.DEBUG, "Exception happened when updating transfer updatetime: %s" % str(error).replace('\n', ''))

    return ret_resps


@transactional_session
def touch_transfer(external_host, transfer_id, session=None):
    """
    Update the timestamp of requests in a transfer. Fails silently if the transfer_id does not exist.
    :param request_host:   Name of the external host.
    :param transfer_id:    External transfer job id as a string.
    :param session:        Database session to use.
    """

    record_counter('core.request.touch_transfer')

    try:
        # don't touch it if it's already touched in 30 seconds
        session.query(models.Request).with_hint(models.Request, "INDEX(REQUESTS REQUESTS_EXTERNALID_UQ)", 'oracle')\
                                     .filter_by(external_id=transfer_id)\
                                     .filter(models.Request.state == RequestState.SUBMITTED)\
                                     .filter(models.Request.updated_at < datetime.datetime.utcnow() - datetime.timedelta(seconds=30))\
                                     .update({'updated_at': datetime.datetime.utcnow()}, synchronize_session=False)
    except IntegrityError as error:
        raise RucioException(error.args)


@transactional_session
def update_transfer_state(external_host, transfer_id, state, session=None, logger=logging.log):
    """
    Used by poller to update the internal state of transfer,
    after the response by the external transfertool.
    :param request_host:          Name of the external host.
    :param transfer_id:           External transfer job id as a string.
    :param state:                 Request state as a string.
    :param session:               The database session to use.
    :param logger:                Optional decorated logger that can be passed from the calling daemons or servers.
    :returns commit_or_rollback:  Boolean.
    """

    try:
        if state == RequestState.LOST:
            reqs = request_core.get_requests_by_transfer(external_host, transfer_id, session=session)
            for req in reqs:
                logger(logging.INFO, 'REQUEST %s OF TRANSFER %s ON %s STATE %s' % (str(req['request_id']), external_host, transfer_id, str(state)))
                src_rse_id = req.get('source_rse_id', None)
                dst_rse_id = req.get('dest_rse_id', None)
                src_rse = None
                dst_rse = None
                if src_rse_id:
                    src_rse = get_rse_name(src_rse_id, session=session)
                if dst_rse_id:
                    dst_rse = get_rse_name(dst_rse_id, session=session)
                response = {'new_state': state,
                            'transfer_id': transfer_id,
                            'job_state': state,
                            'src_url': None,
                            'dst_url': req['dest_url'],
                            'duration': 0,
                            'reason': "The FTS job lost",
                            'scope': req.get('scope', None),
                            'name': req.get('name', None),
                            'src_rse': src_rse,
                            'dst_rse': dst_rse,
                            'request_id': req.get('request_id', None),
                            'activity': req.get('activity', None),
                            'src_rse_id': req.get('source_rse_id', None),
                            'dst_rse_id': req.get('dest_rse_id', None),
                            'previous_attempt_id': req.get('previous_attempt_id', None),
                            'adler32': req.get('adler32', None),
                            'md5': req.get('md5', None),
                            'filesize': req.get('filesize', None),
                            'external_host': external_host,
                            'job_m_replica': None,
                            'created_at': req.get('created_at', None),
                            'submitted_at': req.get('submitted_at', None),
                            'details': None,
                            'account': req.get('account', None)}

                err_msg = request_core.get_transfer_error(response['new_state'], response['reason'] if 'reason' in response else None)
                request_core.set_request_state(req['request_id'],
                                               response['new_state'],
                                               transfer_id=transfer_id,
                                               src_rse_id=src_rse_id,
                                               err_msg=err_msg,
                                               session=session)

                request_core.add_monitor_message(req, response, session=session)
        else:
            __set_transfer_state(external_host, transfer_id, state, session=session)
        return True
    except UnsupportedOperation as error:
        logger(logging.WARNING, "Transfer %s on %s doesn't exist - Error: %s" % (transfer_id, external_host, str(error).replace('\n', '')))
        return False


@transactional_session
def get_hops(source_rse_id, dest_rse_id, include_multihop=False, multihop_rses=None, limit_dest_schemes=None, session=None):
    """
    Get a list of hops needed to transfer date from source_rse_id to dest_rse_id.
    Ideally, the list will only include one item (dest_rse_id) since no hops are needed.
    :param source_rse_id:       Source RSE id of the transfer.
    :param dest_rse_id:         Dest RSE id of the transfer.
    :param include_multihop:    If no direct link can be made, also include multihop transfers.
    :param multihop_rses:       List of RSE ids that can be used for multihop.
    :param limit_dest_schemes:  List of destination schemes the matching scheme algorithm should be limited to for a single hop.
    :returns:                   List of hops in the format [{'source_rse_id': source_rse_id, 'source_scheme': 'srm', 'source_scheme_priority': N, 'dest_rse_id': dest_rse_id, 'dest_scheme': 'srm', 'dest_scheme_priority': N}]
    :raises:                    NoDistance
    """
    if not limit_dest_schemes:
        limit_dest_schemes = []

    # Check if there is a cached result
    result = REGION_SHORT.get('get_hops_%s_%s_%s' % (str(source_rse_id), str(dest_rse_id), ''.join(sorted(limit_dest_schemes))))
    if not isinstance(result, NoValue):
        return result

    if multihop_rses is None:
        multihop_rses = []

    # TODO: Might be problematic to always load the distance_graph, since it might be expensiv

    # Load the graph from the distances table
    # distance_graph = __load_distance_graph(session=session)
    distance_graph = {}
    distance_graph[source_rse_id] = __load_outgoing_distances_node(rse_id=source_rse_id, session=session)

    # 1. Check if there is a direct connection between source and dest:
    if distance_graph.get(source_rse_id, {dest_rse_id: None}).get(dest_rse_id) is not None:
        # Check if there is a protocol match between the two RSEs
        try:
            matching_scheme = rsemgr.find_matching_scheme(rse_settings_dest=__load_rse_settings(rse_id=dest_rse_id, session=session),
                                                          rse_settings_src=__load_rse_settings(rse_id=source_rse_id, session=session),
                                                          operation_src='third_party_copy',
                                                          operation_dest='third_party_copy',
                                                          domain='wan',
                                                          scheme=limit_dest_schemes if limit_dest_schemes else None)
            path = [{'source_rse_id': source_rse_id,
                     'dest_rse_id': dest_rse_id,
                     'source_scheme': matching_scheme[1],
                     'dest_scheme': matching_scheme[0],
                     'source_scheme_priority': matching_scheme[3],
                     'dest_scheme_priority': matching_scheme[2]}]
            REGION_SHORT.set('get_hops_%s_%s_%s' % (str(source_rse_id), str(dest_rse_id), ''.join(sorted(limit_dest_schemes))), path)
            return path
        except RSEProtocolNotSupported as error:
            if include_multihop:
                # Delete the edge from the graph
                del distance_graph[source_rse_id][dest_rse_id]
            else:
                raise error

    if not include_multihop:
        raise NoDistance()

    # 2. There is no connection or no scheme match --> Try a multi hop --> Dijkstra algorithm
    HOP_PENALTY = core_config_get('transfers', 'hop_penalty', default=10, session=session)  # Penalty to be applied to each further hop

    # Check if the destination RSE is an island RSE:
    if not __load_inbound_distances_node(rse_id=dest_rse_id, session=session):
        raise NoDistance()

    visited_nodes = {source_rse_id: {'distance': 0,
                                     'path': []}}  # Dijkstra already visisted nodes
    # {rse_id: {'path': [{'source_rse_id':, 'dest_rse_id':, 'source_scheme', 'dest_scheme': }],
    #           'distance': X}
    # }
    to_visit = [source_rse_id]  # Nodes to visit, once list is empty, break loop
    local_optimum = 9999  # Local optimum to accelerated search

    while to_visit:
        for current_node in copy.deepcopy(to_visit):
            to_visit.remove(current_node)
            current_distance = visited_nodes[current_node]['distance']
            current_path = visited_nodes[current_node]['path']

            if current_node not in distance_graph:
                distance_graph[current_node] = __load_outgoing_distances_node(rse_id=current_node, session=session)

            for out_v in distance_graph[current_node]:
                # Check if the distance would be smaller
                if distance_graph[current_node][out_v] is None:
                    continue
                if visited_nodes.get(out_v, {'distance': 9999})['distance'] > current_distance + distance_graph[current_node][out_v] + HOP_PENALTY\
                   and local_optimum > current_distance + distance_graph[current_node][out_v] + HOP_PENALTY:
                    # Check if the intermediate RSE is enabled for multihop
                    if out_v != dest_rse_id and out_v not in multihop_rses:
                        continue
                    # Check if there is a compatible protocol pair
                    try:
                        matching_scheme = rsemgr.find_matching_scheme(rse_settings_dest=__load_rse_settings(rse_id=out_v, session=session),
                                                                      rse_settings_src=__load_rse_settings(rse_id=current_node, session=session),
                                                                      operation_src='third_party_copy',
                                                                      operation_dest='third_party_copy',
                                                                      domain='wan',
                                                                      scheme=limit_dest_schemes if out_v == dest_rse_id and limit_dest_schemes else None)
                        visited_nodes[out_v] = {'distance': current_distance + distance_graph[current_node][out_v] + HOP_PENALTY,
                                                'path': current_path + [{'source_rse_id': current_node,
                                                                         'dest_rse_id': out_v,
                                                                         'source_scheme': matching_scheme[1],
                                                                         'dest_scheme': matching_scheme[0],
                                                                         'source_scheme_priority': matching_scheme[3],
                                                                         'dest_scheme_priority': matching_scheme[2]}]}
                        if out_v != dest_rse_id:
                            to_visit.append(out_v)
                        else:
                            local_optimum = current_distance + distance_graph[current_node][out_v] + HOP_PENALTY
                    except RSEProtocolNotSupported:
                        pass
    if dest_rse_id in visited_nodes:
        REGION_SHORT.set('get_hops_%s_%s_%s' % (str(source_rse_id), str(dest_rse_id), ''.join(sorted(limit_dest_schemes))), visited_nodes[dest_rse_id]['path'])
        return visited_nodes[dest_rse_id]['path']
    else:
        raise NoDistance()


def get_attributes(attributes):
    dict_attributes = {}
    if attributes:
        if isinstance(attributes, dict):
            attr = json.loads(json.dumps(attributes))
        else:
            attr = json.loads(str(attributes))
    # parse source expression
    dict_attributes['source_replica_expression'] = attr["source_replica_expression"] if (attr and "source_replica_expression" in attr) else None
    dict_attributes['allow_tape_source'] = attr["allow_tape_source"] if (attr and "allow_tape_source" in attr) else True
    dict_attributes['dsn'] = attr["ds_name"] if (attr and "ds_name" in attr) else None
    dict_attributes['lifetime'] = attr.get('lifetime', -1)
    return dict_attributes


def get_dsn(scope, name, dsn):
    if dsn:
        return dsn
    # select a containing dataset
    for parent in did.list_parent_dids(scope, name):
        if parent['type'] == DIDType.DATASET:
            return parent['name']
    return 'other'


def __build_dest_url(scope, name, protocol, dest_rse_attrs, dest_is_deterministic, dest_is_tape, dict_attributes, retry_count, activity):
    """
    Private helper function to build destination URL when retrieving transfers to execute.
    """

    if dest_is_deterministic:
        dest_url = list(protocol.lfns2pfns(lfns={'scope': scope.external, 'name': name}).values())[0]
    else:
        # compute dest url in case of non deterministic
        # naming convention, etc.
        dsn = get_dsn(scope, name, dict_attributes.get('dsn', None))
        # DQ2 path always starts with /, but prefix might not end with /
        naming_convention = dest_rse_attrs.get('naming_convention', None)
        dest_path = construct_surl(dsn, name, naming_convention)
        if dest_is_tape:
            if retry_count or activity == 'Recovery':
                dest_path = '%s_%i' % (dest_path, int(time.time()))

        dest_url = list(protocol.lfns2pfns(lfns={'scope': scope.external, 'name': name, 'path': dest_path}).values())[0]

    return dest_url


def __rewrite_source_url(source_url, source_sign_url, dest_sign_url, source_scheme):
    """
    Parametrize source url for some special cases of source and destination schemes
    """
    if dest_sign_url == 'gcs':
        if source_scheme in ['davs', 'https']:
            source_url += '?copy_mode=push'
    elif dest_sign_url == 's3':
        if source_scheme in ['davs', 'https']:
            source_url += '?copy_mode=push'
    elif WEBDAV_TRANSFER_MODE:
        if source_scheme in ['davs', 'https']:
            source_url += '?copy_mode=%s' % WEBDAV_TRANSFER_MODE

    source_sign_url_map = {'gcs': 'gclouds', 's3': 's3s'}
    if source_sign_url in source_sign_url_map:
        if source_url[:7] == 'davs://':
            source_url = source_sign_url_map[source_sign_url] + source_url[4:]
        if source_url[:8] == 'https://':
            source_url = source_sign_url_map[source_sign_url] + source_url[5:]

    if source_url[:12] == 'srm+https://':
        source_url = 'srm' + source_url[9:]
    return source_url


def __rewrite_dest_url(dest_url, dest_sign_url, dest_scheme):
    """
    Parametrize destination url for some special cases of destination schemes
    """
    if dest_sign_url == 'gcs':
        dest_url = re.sub('davs', 'gclouds', dest_url)
        dest_url = re.sub('https', 'gclouds', dest_url)
    elif dest_sign_url == 's3':
        dest_url = re.sub('davs', 's3s', dest_url)
        dest_url = re.sub('https', 's3s', dest_url)

    if dest_url[:12] == 'srm+https://':
        dest_url = 'srm' + dest_url[9:]
    return dest_url


@transactional_session
def get_transfer_requests_and_source_replicas(total_workers=0, worker_number=0, limit=None, activity=None, older_than=None, rses=None, schemes=None,
                                              bring_online=43200, retry_other_fts=False, failover_schemes=None, transfertool=None, logger=logging.log, session=None):
    """
    Get transfer requests and the associated source replicas
    :param total_workers:         Number of total workers.
    :param worker_number:         Id of the executing worker.
    :param limit:                 Limit.
    :param activity:              Activity.
    :param older_than:            Get transfers older than.
    :param rses:                  Include RSES.
    :param schemes:               Include schemes.
    :param bring_online:          Bring online timeout.
    :param retry_other_fts:       Retry other fts servers.
    :param failover_schemes:      Failover schemes.
    :param transfertool:          The transfer tool as specified in rucio.cfg.
    :param logger:                Optional decorated logger that can be passed from the calling daemons or servers.
    :param session:               The database session in use.
    :returns:                     transfers, reqs_no_source, reqs_scheme_mismatch, reqs_only_tape_source
    """

    req_sources = __list_transfer_requests_and_source_replicas(total_workers=total_workers,
                                                               worker_number=worker_number,
                                                               limit=limit,
                                                               activity=activity,
                                                               older_than=older_than,
                                                               rses=rses,
                                                               request_state=RequestState.QUEUED,
                                                               transfertool=transfertool,
                                                               session=session)

    class _LocalContext:
        def __init__(self, session):
            self.session = session
            self.rse_id_to_name_map = {}
            self.rse_id_to_info_map = {}
            self.rse_id_to_attrs_map = {}
            self.protocols = {}

        def _ensure_rse_loaded(self, rse_id):
            if rse_id not in self.rse_id_to_name_map:
                rse_name = get_rse_name(rse_id=rse_id, session=self.session)
                self.rse_id_to_name_map[rse_id] = rse_name
                self.rse_id_to_info_map[rse_id] = rsemgr.get_rse_info(rse=rse_name,
                                                                      vo=get_rse_vo(rse_id=rse_id, session=self.session),
                                                                      session=self.session)
                self.rse_id_to_attrs_map[rse_id] = get_rse_attributes(rse_id, session=self.session)

        def rse_name(self, rse_id):
            self._ensure_rse_loaded(rse_id)
            return self.rse_id_to_name_map[rse_id]

        def rse_info(self, rse_id):
            self._ensure_rse_loaded(rse_id)
            return self.rse_id_to_info_map[rse_id]

        def rse_attrs(self, rse_id):
            self._ensure_rse_loaded(rse_id)
            return self.rse_id_to_attrs_map[rse_id]

        def is_tape_rse(self, rse_id):
            _rse_info = self.rse_info(rse_id)
            if _rse_info['rse_type'] == RSEType.TAPE or _rse_info['rse_type'] == 'TAPE':
                return True
            return False

        def protocol(self, rse_id, scheme, operation):
            protocol_key = '%s_%s_%s' % (operation, rse_id, scheme)
            protocol = self.protocols.get(protocol_key)
            if not protocol:
                protocol = rsemgr.create_protocol(self.rse_info(rse_id), 'third_party_copy', scheme)
                self.protocols[protocol_key] = protocol
            return protocol

    ctx = _LocalContext(session)
    unavailable_read_rse_ids = __get_unavailable_rse_ids(operation='read', session=session)
    unavailable_write_rse_ids = __get_unavailable_rse_ids(operation='write', session=session)

    bring_online_local = bring_online
    transfers, reqs_no_source, reqs_only_tape_source, reqs_scheme_mismatch = {}, [], [], []
    multi_hop_dict = {}

    multihop_rses = []
    try:
        multihop_rses = [rse['id'] for rse in parse_expression('available_for_multihop=true')]
    except InvalidRSEExpression:
        multihop_rses = []

    for req_id, rule_id, scope, name, md5, adler32, bytes, activity, attributes, previous_attempt_id, dest_rse_id, account, source_rse_id, rse, deterministic, rse_type, path, retry_count, src_url, ranking, link_ranking in req_sources:

        if ranking is None:
            ranking = 0

        multihop = False

        # Add req to req_no_source list (Will be removed later if needed)
        if req_id not in reqs_no_source:
            reqs_no_source.append(req_id)

        # source_rse_id will be None if no source replicas
        # rse will be None if rse is staging area
        if source_rse_id is None or rse is None:
            continue

        if rses and dest_rse_id not in rses:
            continue

        dest_rse_name = ctx.rse_name(dest_rse_id)
        source_rse_name = ctx.rse_name(source_rse_id)

        dict_attributes = get_attributes(attributes)

        # Check if the source and destination are blocked
        if source_rse_id in unavailable_read_rse_ids:
            continue
        if dest_rse_id in unavailable_write_rse_ids:
            logger(logging.WARNING, 'RSE %s is blocked for write. Will skip the submission of new jobs', dest_rse_name)
            continue

        # parse source expression
        source_replica_expression = dict_attributes.get('source_replica_expression', None)
        if source_replica_expression:
            try:
                parsed_rses = parse_expression(source_replica_expression, session=session)
            except InvalidRSEExpression as error:
                logger(logging.ERROR, "Invalid RSE exception %s: %s", source_replica_expression, str(error))
                continue
            else:
                allowed_rses = [x['id'] for x in parsed_rses]
                if source_rse_id not in allowed_rses:
                    continue

        # Call the get_hops function to create a list of RSEs used for the transfer
        # In case the source_rse and the dest_rse are connected, the list contains only the destination RSE
        # In case of non-connected, the list contains all the intermediary RSEs
        list_hops = []
        include_multihop = False
        if transfertool in ['fts3', None]:
            include_multihop = core_config_get('transfers', 'use_multihop', default=False, expiration_time=600, session=session)

        try:
            list_hops = get_hops(source_rse_id,
                                 dest_rse_id,
                                 include_multihop=include_multihop,
                                 multihop_rses=multihop_rses,
                                 limit_dest_schemes=transfers.get(req_id, {}).get('schemes', None),
                                 session=session)
            if len(list_hops) > 1:
                logger(logging.DEBUG, 'From %s to %s requires multihop: %s', source_rse_id, dest_rse_id, list_hops)
                multihop = True
                multi_hop_dict[req_id] = (list_hops, dict_attributes, retry_count)
        except NoDistance:
            logger(logging.WARNING, "Request %s: no link from %s to %s", req_id, source_rse_name, dest_rse_name)
            if req_id in reqs_scheme_mismatch:
                reqs_scheme_mismatch.remove(req_id)
            if req_id not in reqs_no_source:
                reqs_no_source.append(req_id)
            continue
        except RSEProtocolNotSupported:
            logger(logging.WARNING, "Request %s: no matching protocol between %s and %s", req_id, source_rse_name, dest_rse_name)
            if req_id in reqs_no_source:
                reqs_no_source.remove(req_id)
            if req_id not in reqs_scheme_mismatch:
                reqs_scheme_mismatch.append(req_id)
            continue

        source_scheme = list_hops[0]['source_scheme']
        dest_scheme = list_hops[-1]['dest_scheme']
        dest_scheme_priority = list_hops[-1]['dest_scheme_priority']

        allow_tape_source = True
        try:
            # Get source protocol
            source_protocol = ctx.protocol(source_rse_id, source_scheme, 'read')

            source_sign_url = ctx.rse_attrs(source_rse_id).get('sign_url', None)
            dest_sign_url = ctx.rse_attrs(dest_rse_id).get('sign_url', None)

            # Compute the source URL
            source_url = list(source_protocol.lfns2pfns(lfns={'scope': scope.external, 'name': name, 'path': path}).values())[0]
            source_url = __rewrite_source_url(source_url, source_sign_url=source_sign_url, dest_sign_url=dest_sign_url, source_scheme=source_scheme)

            # If the request_id is not already in the transfer dictionary, need to compute the destination URL
            if req_id not in transfers:

                # parse allow tape source expression, not finally version.
                # allow_tape_source = attr["allow_tape_source"] if (attr and "allow_tape_source" in attr) else True
                allow_tape_source = True

                # Extend the metadata dictionary with request attributes
                transfer_src_type = "DISK"
                transfer_dst_type = "DISK"
                overwrite, bring_online = True, None
                if ctx.is_tape_rse(source_rse_id) or ctx.rse_attrs(source_rse_id).get('staging_required', False):
                    bring_online = bring_online_local
                    transfer_src_type = "TAPE"
                    if not allow_tape_source:
                        if req_id not in reqs_only_tape_source:
                            reqs_only_tape_source.append(req_id)
                        if req_id in reqs_no_source:
                            reqs_no_source.remove(req_id)
                        continue

                if ctx.is_tape_rse(dest_rse_id):
                    overwrite = False
                    transfer_dst_type = "TAPE"

                # Get destination protocol
                dest_protocol = ctx.protocol(dest_rse_id, dest_scheme, 'write')

                # Compute the destination url
                dest_url = __build_dest_url(scope=scope, name=name,
                                            protocol=dest_protocol,
                                            dest_rse_attrs=ctx.rse_attrs(dest_rse_id),
                                            dest_is_deterministic=ctx.rse_info(dest_rse_id)['deterministic'],
                                            dest_is_tape=ctx.is_tape_rse(dest_rse_id),
                                            dict_attributes=dict_attributes,
                                            retry_count=retry_count,
                                            activity=activity)
                dest_url = __rewrite_dest_url(dest_url, dest_sign_url=dest_sign_url, dest_scheme=dest_scheme)

                # Get dest space token
                dest_spacetoken = None
                if dest_protocol.attributes and 'extended_attributes' in dest_protocol.attributes and \
                        dest_protocol.attributes['extended_attributes'] and 'space_token' in dest_protocol.attributes['extended_attributes']:
                    dest_spacetoken = dest_protocol.attributes['extended_attributes']['space_token']

                use_ipv4 = ctx.rse_attrs(source_rse_id).get('use_ipv4', False) or ctx.rse_attrs(dest_rse_id).get('use_ipv4', False)

                # get external_host + strict_copy + archive timeout
                strict_copy = ctx.rse_attrs(dest_rse_id).get('strict_copy', False)
                fts_hosts = ctx.rse_attrs(dest_rse_id).get('fts', None)
                archive_timeout = ctx.rse_attrs(dest_rse_id).get('archive_timeout', None)
                if source_sign_url == 'gcs':
                    fts_hosts = ctx.rse_attrs(source_rse_id).get('fts', None)
                source_globus_endpoint_id = ctx.rse_attrs(source_rse_id).get('globus_endpoint_id', None)
                dest_globus_endpoint_id = ctx.rse_attrs(dest_rse_id).get('globus_endpoint_id', None)

                if transfertool == 'fts3' and not fts_hosts:
                    logger(logging.ERROR, 'Destination RSE %s FTS attribute not defined - SKIP REQUEST %s', dest_rse_name, req_id)
                    continue
                if transfertool == 'globus' and (not dest_globus_endpoint_id or not source_globus_endpoint_id):
                    logger(logging.ERROR, 'Destination RSE %s Globus endpoint attributes not defined - SKIP REQUEST %s', dest_rse_name, req_id)
                    continue
                if retry_count is None:
                    retry_count = 0
                external_host = ''
                if fts_hosts:
                    fts_list = fts_hosts.split(",")
                    external_host = fts_list[0]

                if retry_other_fts:
                    external_host = fts_list[retry_count % len(fts_list)]

                # Get the checksum validation strategy (none, source, destination or both)
                verify_checksum = 'both'
                if not ctx.rse_attrs(dest_rse_id).get('verify_checksum', True):
                    if not ctx.rse_attrs(source_rse_id).get('verify_checksum', True):
                        verify_checksum = 'none'
                    else:
                        verify_checksum = 'source'
                else:
                    if not ctx.rse_attrs(source_rse_id).get('verify_checksum', True):
                        verify_checksum = 'destination'
                    else:
                        verify_checksum = 'both'

                source_rse_checksums = get_rse_supported_checksums(source_rse_id, session=session)
                dest_rse_checksums = get_rse_supported_checksums(dest_rse_id, session=session)

                common_checksum_names = set(source_rse_checksums).intersection(dest_rse_checksums)

                if len(common_checksum_names) == 0:
                    logger(logging.INFO, 'No common checksum method. Verifying destination only.')
                    verify_checksum = 'destination'

                # Fill the transfer dictionary including file_metadata
                file_metadata = {'request_id': req_id,
                                 'scope': scope,
                                 'name': name,
                                 'activity': activity,
                                 'request_type': RequestType.TRANSFER,
                                 'src_type': transfer_src_type,
                                 'dst_type': transfer_dst_type,
                                 'src_rse': source_rse_name,
                                 'dst_rse': dest_rse_name,
                                 'src_rse_id': source_rse_id,
                                 'dest_rse_id': dest_rse_id,
                                 'filesize': bytes,
                                 'md5': md5,
                                 'adler32': adler32,
                                 'verify_checksum': verify_checksum,
                                 'source_globus_endpoint_id': source_globus_endpoint_id,
                                 'dest_globus_endpoint_id': dest_globus_endpoint_id}

                if previous_attempt_id:
                    file_metadata['previous_attempt_id'] = previous_attempt_id

                transfers[req_id] = {'request_id': req_id,
                                     'schemes': __add_compatible_schemes(schemes=[dest_scheme], allowed_schemes=SUPPORTED_PROTOCOLS),
                                     'account': account,
                                     # 'src_urls': [source_url],
                                     'sources': [(rse, source_url, source_rse_id, ranking, link_ranking)],
                                     'dest_urls': [dest_url],
                                     'src_spacetoken': None,
                                     'dest_spacetoken': dest_spacetoken,
                                     'overwrite': overwrite,
                                     'bring_online': bring_online,
                                     'copy_pin_lifetime': dict_attributes.get('lifetime', 172800),
                                     'external_host': external_host,
                                     'selection_strategy': 'auto',
                                     'rule_id': rule_id,
                                     'file_metadata': file_metadata,
                                     'dest_scheme_priority': dest_scheme_priority}
                if multihop:
                    transfers[req_id]['multihop'] = True
                    transfers[req_id]['initial_request_id'] = req_id
                if strict_copy:
                    transfers[req_id]['strict_copy'] = strict_copy
                if use_ipv4:
                    transfers[req_id]['use_ipv4'] = True
                if archive_timeout and ctx.is_tape_rse(dest_rse_id):
                    try:
                        transfers[req_id]['archive_timeout'] = int(archive_timeout)
                        logger(logging.DEBUG, 'Added archive timeout to transfer.')
                    except ValueError:
                        logger(logging.WARNING, 'Could not set archive_timeout for %s. Must be integer.', dest_url)
                        pass
            else:
                # parse allow tape source expression, not finally version.
                allow_tape_source = dict_attributes.get('allow_tape_source', None)

                # No check yet if the previous one is a multihop or not.
                # TODO : Check if the current  transfer is better than the previous one
                if multihop:
                    continue

                current_source_is_tape = transfers[req_id]['bring_online']
                new_source_is_tape = ctx.is_tape_rse(source_rse_id) or ctx.rse_attrs(source_rse_id).get('staging_required', False)

                if new_source_is_tape and not allow_tape_source:
                    continue

                if current_source_is_tape and not new_source_is_tape or \
                        new_source_is_tape and not current_source_is_tape:
                    # Tape and Disk sources must not be used at the same time.
                    # Either keep existing sources unchanged, or substitute all existing source with the new source.

                    # Find the best ranking among existing sources
                    avail_top_ranking = None
                    for founded_source in transfers[req_id]['sources']:
                        if avail_top_ranking is None:
                            avail_top_ranking = founded_source[3]
                            continue
                        if founded_source[3] is not None and founded_source[3] > avail_top_ranking:
                            avail_top_ranking = founded_source[3]

                    # If ranking of the new source is better. On equal ranking, prefer Disk over Tape.
                    if avail_top_ranking is None or (ranking > avail_top_ranking) or (ranking >= avail_top_ranking and current_source_is_tape):
                        transfers[req_id]['sources'] = []
                        transfers[req_id]['bring_online'] = bring_online_local if new_source_is_tape else None
                        transfers[req_id]['file_metadata']['src_type'] = 'TAPE' if new_source_is_tape else 'DISK'
                        transfers[req_id]['file_metadata']['src_rse'] = rse
                    else:
                        continue

                if current_source_is_tape and new_source_is_tape:
                    # multiple Tape source replicas are not allowed in FTS3.
                    # Either keep the old source. Or substitute it with the new one.
                    prev_is_multihop = transfers[req_id]['sources'][0][4] is None  # will be None if the previous transfer is multihop
                    if ranking > transfers[req_id]['sources'][0][3]\
                            or (ranking == transfers[req_id]['sources'][0][3] and (prev_is_multihop
                                                                                   or link_ranking < transfers[req_id]['sources'][0][4])):
                        transfers[req_id]['sources'] = []
                        transfers[req_id]['bring_online'] = bring_online_local
                        transfers[req_id]['file_metadata']['src_rse'] = rse
                    else:
                        continue

                # The transfer queued previously is a multihop, but this one is direct.
                # Reset the sources, remove the multihop flag
                if transfers[req_id].get('multihop', False):
                    transfers[req_id].pop('multihop', None)
                    transfers[req_id]['sources'] = []

                transfers[req_id]['sources'].append((rse, source_url, source_rse_id, ranking, link_ranking))
                # if one source has force IPv4, force IPv4 for the whole job
                use_ipv4 = ctx.rse_attrs(source_rse_id).get('use_ipv4', False)
                if use_ipv4:
                    transfers[req_id]['use_ipv4'] = True

        except Exception:
            logger(logging.CRITICAL, "Exception happened when trying to get transfer for request %s:" % (req_id), exc_info=True)
            break

    # checking OIDC AuthN/Z support per destination and soucre RSEs;
    # assumes use of boolean 'oidc_support' RSE attribute
    for req_id in transfers:
        use_oidc = False
        dest_rse_id = transfers[req_id]['file_metadata']['dest_rse_id']
        if 'oidc_support' in ctx.rse_attrs(dest_rse_id):
            use_oidc = ctx.rse_attrs(dest_rse_id)['oidc_support']
        else:
            transfers[req_id]['use_oidc'] = use_oidc
            continue
        for source in transfers[req_id]['sources']:
            source_rse_id = source[2]
            if 'oidc_support' in ctx.rse_attrs(source_rse_id):
                use_oidc = use_oidc and ctx.rse_attrs(source_rse_id)['oidc_support']
            else:
                use_oidc = False
            if not use_oidc:
                break
        # OIDC token will be requested for the account of this tranfer
        transfers[req_id]['use_oidc'] = use_oidc

    for req_id in copy.deepcopy(transfers):
        # If the transfer is a multihop, need to create the intermediate replicas, intermediate requests and the transfers
        if transfers[req_id].get('multihop', False):
            parent_request = None
            scope = transfers[req_id]['file_metadata']['scope']
            name = transfers[req_id]['file_metadata']['name']
            list_multihop, dict_attributes, retry_count = multi_hop_dict[req_id]
            parent_requests = []

            for hop in list_multihop:
                # hop = {'source_rse_id': source_rse_id, 'source_scheme': 'srm', 'source_scheme_priority': N, 'dest_rse_id': dest_rse_id, 'dest_scheme': 'srm', 'dest_scheme_priority': N}
                source_scheme = hop['source_scheme']
                source_rse_id = hop['source_rse_id']
                dest_rse_id = hop['dest_rse_id']
                source_rse_name = ctx.rse_name(source_rse_id)
                dest_rse_name = ctx.rse_name(dest_rse_id)
                dest_rse_vo = get_rse_vo(rse_id=hop['dest_rse_id'], session=session)
                transfer_src_type = "DISK"
                transfer_dst_type = "DISK"
                allow_tape_source = True
                # Compute the source URL. We don't need to fill the rse_mapping and rse_attrs for the intermediate RSEs cause it has already been done before
                source_protocol = ctx.protocol(source_rse_id, source_scheme, 'read')
                source_url = list(source_protocol.lfns2pfns(lfns={'scope': scope.external, 'name': name, 'path': None}).values())[0]

                if transfers[req_id]['file_metadata']['dest_rse_id'] != hop['dest_rse_id']:
                    files = [{'scope': scope,
                              'name': name,
                              'bytes': transfers[req_id]['file_metadata']['filesize'],
                              'adler32': transfers[req_id]['file_metadata']['adler32'],
                              'md5': transfers[req_id]['file_metadata']['md5'],
                              'state': 'C'}]
                    try:
                        add_replicas(rse_id=hop['dest_rse_id'],
                                     files=files,
                                     account=InternalAccount('root', vo=dest_rse_vo),
                                     ignore_availability=False,
                                     dataset_meta=None,
                                     session=session)
                    except Exception as error:
                        logger(logging.ERROR, 'Problem adding replicas %s:%s on %s : %s', scope, name, dest_rse_name, str(error))

                    req_attributes = {'activity': transfers[req_id]['file_metadata']['activity'],
                                      'source_replica_expression': None,
                                      'lifetime': None,
                                      'ds_scope': None,
                                      'ds_name': None,
                                      'bytes': transfers[req_id]['file_metadata']['filesize'],
                                      'md5': transfers[req_id]['file_metadata']['md5'],
                                      'adler32': transfers[req_id]['file_metadata']['adler32'],
                                      'priority': None,
                                      'allow_tape_source': True}
                    new_req = queue_requests(requests=[{'dest_rse_id': dest_rse_id,
                                                        'scope': scope,
                                                        'name': name,
                                                        'rule_id': '00000000000000000000000000000000',  # Dummy Rule ID used for multihop. TODO: Replace with actual rule_id once we can flag intermediate requests
                                                        'attributes': req_attributes,
                                                        'request_type': RequestType.TRANSFER,
                                                        'retry_count': retry_count,
                                                        'account': InternalAccount('root', vo=dest_rse_vo),
                                                        'requested_at': datetime.datetime.now()}], session=session)
                    # If a request already exists, new_req will be an empty list.
                    if not new_req:
                        # Need to fail all the intermediate requests + the initial one and exit the multihop loop
                        logger(logging.WARNING, 'Multihop : A request already exists for the transfer between %s and %s. Will cancel all the parent requests', source_rse_name, dest_rse_name)
                        parent_requests.append(req_id)
                        try:
                            set_requests_state(request_ids=parent_requests, new_state=RequestState.FAILED, session=session)
                        except UnsupportedOperation:
                            logger(logging.ERROR, 'Multihop : Cannot cancel all the parent requests : %s', str(parent_requests))

                        # Remove from the transfer dictionary all the requests
                        for cur_req_id in parent_requests:
                            transfers.pop(cur_req_id, None)
                        break
                    new_req_id = new_req[0]['id']
                    parent_requests.append(new_req_id)
                    set_requests_state(request_ids=[new_req_id, ], new_state=RequestState.QUEUED, session=session)
                    logger(logging.DEBUG, 'New request created for the transfer between %s and %s : %s', source_rse_name, dest_rse_name, new_req_id)

                    # Here we will compute the destination URL
                    # Get destination protocol
                    dest_rse_id = hop['dest_rse_id']
                    dest_scheme = hop['dest_scheme']
                    dest_protocol = ctx.protocol(dest_rse_id, dest_scheme, 'write')

                    # Get dest space token
                    dest_spacetoken = None
                    if dest_protocol.attributes and 'extended_attributes' in dest_protocol.attributes and \
                            dest_protocol.attributes['extended_attributes'] and 'space_token' in dest_protocol.attributes['extended_attributes']:
                        dest_spacetoken = dest_protocol.attributes['extended_attributes']['space_token']

                    # Compute the destination url
                    dest_url = __build_dest_url(scope=scope, name=name,
                                                protocol=dest_protocol,
                                                dest_rse_attrs=ctx.rse_attrs(dest_rse_id),
                                                dest_is_deterministic=ctx.rse_info(dest_rse_id)['deterministic'],
                                                dest_is_tape=ctx.is_tape_rse(dest_rse_id),
                                                dict_attributes=dict_attributes,
                                                retry_count=retry_count,
                                                activity=activity)

                    # Extend the metadata dictionary with request attributes
                    overwrite, bring_online = True, None
                    if ctx.is_tape_rse(source_rse_id):
                        bring_online = bring_online_local
                        transfer_src_type = "TAPE"
                        if not allow_tape_source:
                            if req_id not in reqs_only_tape_source:
                                reqs_only_tape_source.append(req_id)
                            if req_id in reqs_no_source:
                                reqs_no_source.remove(req_id)
                            continue
                    if ctx.is_tape_rse(dest_rse_id):
                        overwrite = False
                        transfer_dst_type = "TAPE"

                    file_metadata = {'request_id': new_req_id,
                                     'scope': scope,
                                     'name': name,
                                     'activity': transfers[req_id]['file_metadata']['activity'],
                                     'request_type': RequestType.TRANSFER,
                                     'src_type': transfer_src_type,
                                     'dst_type': transfer_dst_type,
                                     'src_rse': source_rse_name,
                                     'dst_rse': dest_rse_name,
                                     'src_rse_id': source_rse_id,
                                     'dest_rse_id': dest_rse_id,
                                     'filesize': transfers[req_id]['file_metadata']['filesize'],
                                     'md5': transfers[req_id]['file_metadata']['md5'],
                                     'adler32': transfers[req_id]['file_metadata']['adler32'],
                                     'verify_checksum': transfers[req_id]['file_metadata']['verify_checksum'],
                                     'source_globus_endpoint_id': transfers[req_id]['file_metadata']['source_globus_endpoint_id'],
                                     'dest_globus_endpoint_id': transfers[req_id]['file_metadata']['dest_globus_endpoint_id']}
                    transfers[new_req_id] = {'request_id': new_req_id,
                                             'initial_request_id': req_id,
                                             'parent_request': parent_request,
                                             'account': InternalAccount('root'),
                                             'schemes': __add_compatible_schemes(schemes=[dest_scheme], allowed_schemes=SUPPORTED_PROTOCOLS),
                                             # 'src_urls': [source_url],
                                             'sources': [(source_rse_name, source_url, source_rse_id, 0, 0)],
                                             'dest_urls': [dest_url],
                                             'src_spacetoken': None,
                                             'dest_spacetoken': dest_spacetoken,
                                             'overwrite': transfers[req_id]['overwrite'],
                                             'bring_online': bring_online,
                                             'copy_pin_lifetime': transfers[req_id]['copy_pin_lifetime'],
                                             'external_host': transfers[req_id]['external_host'],
                                             'selection_strategy': 'auto',
                                             'rule_id': transfers[req_id]['rule_id'],
                                             'multihop': True,
                                             'file_metadata': file_metadata}
                    parent_request = new_req_id

                else:
                    # For the last hop, we just need to correct the source
                    transfers[req_id]['parent_request'] = parent_request
                    transfers[req_id]['file_metadata']['src_rse_id'] = source_rse_id
                    transfers[req_id]['file_metadata']['src_rse'] = source_rse_name
                    # We make the assumption that the hop is never made through TAPE
                    transfers[req_id]['file_metadata']['src_type'] = 'DISK'
                    transfers[req_id]['sources'] = [(source_rse_name, source_url, source_rse_id, 0, 0)]
                    transfers[req_id]['bring_online'] = bring_online
        if req_id in reqs_no_source:
            reqs_no_source.remove(req_id)
        if req_id in reqs_only_tape_source:
            reqs_only_tape_source.remove(req_id)
        if req_id in reqs_scheme_mismatch:
            reqs_scheme_mismatch.remove(req_id)

    return transfers, reqs_no_source, reqs_scheme_mismatch, reqs_only_tape_source


@read_session
def __list_transfer_requests_and_source_replicas(total_workers=0, worker_number=0, limit=None, activity=None,
                                                 older_than=None, rses=None, request_state=None, transfertool=None, session=None) -> "List[Tuple]":
    """
    List requests with source replicas
    :param total_workers:     Number of total workers.
    :param worker_number:     Id of the executing worker.
    :param limit:            Integer of requests to retrieve.
    :param activity:         Activity to be selected.
    :param older_than:       Only select requests older than this DateTime.
    :param rses:             List of rse_id to select requests.
    :param transfertool:     The transfer tool as specified in rucio.cfg.
    :param session:          Database session to use.
    :returns:                List.
    """

    if request_state is None:
        request_state = RequestState.QUEUED

    sub_requests = session.query(models.Request.id,
                                 models.Request.rule_id,
                                 models.Request.scope,
                                 models.Request.name,
                                 models.Request.md5,
                                 models.Request.adler32,
                                 models.Request.bytes,
                                 models.Request.activity,
                                 models.Request.attributes,
                                 models.Request.previous_attempt_id,
                                 models.Request.dest_rse_id,
                                 models.Request.retry_count,
                                 models.Request.account,
                                 models.Request.created_at) \
        .with_hint(models.Request, "INDEX(REQUESTS REQUESTS_TYP_STA_UPD_IDX)", 'oracle') \
        .filter(models.Request.state == request_state) \
        .filter(models.Request.request_type == RequestType.TRANSFER) \
        .join(models.RSE, models.RSE.id == models.Request.dest_rse_id) \
        .filter(models.RSE.deleted == false()) \
        .order_by(models.Request.created_at) \
        .filter(models.RSE.availability.in_((2, 3, 6, 7)))

    if isinstance(older_than, datetime.datetime):
        sub_requests = sub_requests.filter(models.Request.requested_at < older_than)

    if activity:
        sub_requests = sub_requests.filter(models.Request.activity == activity)

    # if a transfertool is specified make sure to filter for those requests and apply related index
    if transfertool:
        sub_requests = sub_requests.filter(models.Request.transfertool == transfertool)
        sub_requests = sub_requests.with_hint(models.Request, "INDEX(REQUESTS REQUESTS_TYP_STA_TRA_ACT_IDX)", 'oracle')
    else:
        sub_requests = sub_requests.with_hint(models.Request, "INDEX(REQUESTS REQUESTS_TYP_STA_UPD_IDX)", 'oracle')

    sub_requests = filter_thread_work(session=session, query=sub_requests, total_threads=total_workers, thread_id=worker_number, hash_variable='requests.id')

    if limit:
        sub_requests = sub_requests.limit(limit)

    sub_requests = sub_requests.subquery()

    query = session.query(sub_requests.c.id,
                          sub_requests.c.rule_id,
                          sub_requests.c.scope,
                          sub_requests.c.name,
                          sub_requests.c.md5,
                          sub_requests.c.adler32,
                          sub_requests.c.bytes,
                          sub_requests.c.activity,
                          sub_requests.c.attributes,
                          sub_requests.c.previous_attempt_id,
                          sub_requests.c.dest_rse_id,
                          sub_requests.c.account,
                          models.RSEFileAssociation.rse_id,
                          models.RSE.rse,
                          models.RSE.deterministic,
                          models.RSE.rse_type,
                          models.RSEFileAssociation.path,
                          sub_requests.c.retry_count,
                          models.Source.url,
                          models.Source.ranking.label("source_ranking"),
                          models.Distance.ranking.label("distance_ranking")) \
        .order_by(sub_requests.c.created_at) \
        .outerjoin(models.RSEFileAssociation, and_(sub_requests.c.scope == models.RSEFileAssociation.scope,
                                                   sub_requests.c.name == models.RSEFileAssociation.name,
                                                   models.RSEFileAssociation.state == ReplicaState.AVAILABLE,
                                                   sub_requests.c.dest_rse_id != models.RSEFileAssociation.rse_id)) \
        .with_hint(models.RSEFileAssociation, "+ index(replicas REPLICAS_PK)", 'oracle') \
        .outerjoin(models.RSE, and_(models.RSE.id == models.RSEFileAssociation.rse_id,
                                    models.RSE.deleted == false())) \
        .outerjoin(models.Source, and_(sub_requests.c.id == models.Source.request_id,
                                       models.RSE.id == models.Source.rse_id)) \
        .with_hint(models.Source, "+ index(sources SOURCES_PK)", 'oracle') \
        .outerjoin(models.Distance, and_(sub_requests.c.dest_rse_id == models.Distance.dest_rse_id,
                                         models.RSEFileAssociation.rse_id == models.Distance.src_rse_id)) \
        .with_hint(models.Distance, "+ index(distances DISTANCES_PK)", 'oracle')

    # if transfertool specified, select only the requests where the source rses are set up for the transfer tool
    if transfertool:
        query = query.subquery()
        query = session.query(query) \
            .join(models.RSEAttrAssociation, models.RSEAttrAssociation.rse_id == query.c.rse_id) \
            .filter(models.RSEAttrAssociation.key == 'transfertool',
                    models.RSEAttrAssociation.value.like('%' + transfertool + '%'))

    if rses:
        result = []
        for item in query.all():
            dest_rse_id = item[10]
            if dest_rse_id in rses:
                result.append(item)
        return result
    return query.all()


@transactional_session
def __set_transfer_state(external_host, transfer_id, new_state, session=None):
    """
    Update the state of a transfer. Fails silently if the transfer_id does not exist.
    :param external_host:  Selected external host as string in format protocol://fqdn:port
    :param transfer_id:    External transfer job id as a string.
    :param new_state:      New state as string.
    :param session:        Database session to use.
    """

    record_counter('core.request.set_transfer_state')

    try:
        rowcount = session.query(models.Request).filter_by(external_id=transfer_id).update({'state': new_state, 'updated_at': datetime.datetime.utcnow()}, synchronize_session=False)
    except IntegrityError as error:
        raise RucioException(error.args)

    if not rowcount:
        raise UnsupportedOperation("Transfer %s on %s state %s cannot be updated." % (transfer_id, external_host, new_state))


@read_session
def __get_unavailable_rse_ids(operation, session=None, logger=logging.log):
    """
    :param logger:   Optional decorated logger that can be passed from the calling daemons or servers.
    Get unavailable rse ids for a given operation : read, write, delete
    """

    if operation not in ['read', 'write', 'delete']:
        logger(logging.ERROR, "Wrong operation specified : %s" % (operation))
        return []
    key = 'unavailable_%s_rse_ids' % operation
    result = REGION_SHORT.get(key)
    if isinstance(result, NoValue):
        try:
            logger(logging.DEBUG, "Refresh unavailable %s rses" % operation)
            availability_key = 'availability_%s' % operation
            unavailable_rses = list_rses(filters={availability_key: False}, session=session)
            unavailable_rse_ids = [rse['id'] for rse in unavailable_rses]
            REGION_SHORT.set(key, unavailable_rse_ids)
            return unavailable_rse_ids
        except Exception:
            logger(logging.ERROR, "Failed to refresh unavailable %s rses, error" % (operation), exc_info=True)
            return []
    return result


def __add_compatible_schemes(schemes, allowed_schemes):
    """
    Add the compatible schemes to a list of schemes
    :param schemes:           Schemes as input.
    :param allowed_schemes:   Allowed schemes, only these can be in the output.
    :returns:                 List of schemes
    """

    return_schemes = []
    for scheme in schemes:
        if scheme in allowed_schemes:
            return_schemes.append(scheme)
            for scheme_map_scheme in constants.SCHEME_MAP.get(scheme, []):
                if scheme_map_scheme not in allowed_schemes:
                    continue
                else:
                    return_schemes.append(scheme_map_scheme)
    return list(set(return_schemes))


@transactional_session
def __load_inbound_distances_node(rse_id, session=None):
    """
    Loads the inbound edges of the distance graph for one node.
    :param rse_id:    RSE id to load the edges for.
    :param session:   The DB Session to use.
    :returns:         Dictionary based graph object.
    """

    result = REGION_SHORT.get('inbound_edges_%s' % str(rse_id))
    if isinstance(result, NoValue):
        inbound_edges = {}
        for distance in session.query(models.Distance).join(models.RSE, models.RSE.id == models.Distance.src_rse_id) \
                .filter(models.Distance.dest_rse_id == rse_id) \
                .filter(models.RSE.deleted == false()).all():
            if distance.ranking is None:
                continue
            ranking = distance.ranking if distance.ranking >= 0 else 0
            inbound_edges[distance.src_rse_id] = ranking
        REGION_SHORT.set('inbound_edges_%s' % str(rse_id), inbound_edges)
        result = inbound_edges
    return result


@transactional_session
def __load_outgoing_distances_node(rse_id, session=None):
    """
    Loads the outgoing edges of the distance graph for one node.
    :param rse_id:    RSE id to load the edges for.
    :param session:   The DB Session to use.
    :returns:         Dictionary based graph object.
    """

    result = REGION_SHORT.get('outgoing_edges_%s' % str(rse_id))
    if isinstance(result, NoValue):
        outgoing_edges = {}
        for distance in session.query(models.Distance).join(models.RSE, models.RSE.id == models.Distance.dest_rse_id)\
                               .filter(models.Distance.src_rse_id == rse_id)\
                               .filter(models.RSE.deleted == false()).all():
            if distance.ranking is None:
                continue
            ranking = distance.ranking if distance.ranking >= 0 else 0
            outgoing_edges[distance.dest_rse_id] = ranking
        REGION_SHORT.set('outgoing_edges_%s' % str(rse_id), outgoing_edges)
        result = outgoing_edges
    return result


@transactional_session
def __load_rse_settings(rse_id, session=None):
    """
    Loads the RSE settings from cache.
    :param rse_id:    RSE id to load the settings from.
    :param session:   The DB Session to use.
    :returns:         Dict of RSE Settings
    """

    result = REGION_SHORT.get('rse_settings_%s' % str(rse_id))
    if isinstance(result, NoValue):
        result = rsemgr.get_rse_info(rse=get_rse_name(rse_id=rse_id, session=session),
                                     vo=get_rse_vo(rse_id=rse_id, session=session),
                                     session=session)
        REGION_SHORT.set('rse_settings_%s' % str(rse_id), result)
    return result
