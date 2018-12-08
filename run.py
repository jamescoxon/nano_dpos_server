# MIT License
#
# Copyright (c) 2018 James Coxon
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import nanoutils as nano
from interface_client import InterfaceClient
import tornado.httpserver
import tornado.websocket
import tornado.ioloop
import tornado.web
from tornado import gen
import logging
import socket
import requests
import json
import time
import datetime
import argparse
import random
import rethinkdb
import hashlib
import uuid
from enum import Enum

# from tornado.concurrent import Future, chain_future

TIMEOUT_ON_DEMAND = 6.0

parser = argparse.ArgumentParser()
parser.add_argument("--rai_node_uri", help='rai_nodes uri, usually 127.0.0.1', default='127.0.0.1')
parser.add_argument("--rai_node_port", help='rai_node port, usually 7076', default='7076')
parser.add_argument("--internal_port", help='internal port which nginx proxys', default='5000')
parser.add_argument("--interface", help='send data to interface (configured in interface.cfg', action='store_true')
parser.add_argument("-v", "--verbose", help='more prints to help debugging', action='store_true')

args = parser.parse_args()

rai_node_address = 'http://{uri}:{port}'.format(uri=args.rai_node_uri, port=args.rai_node_port)

wss_demand = []
wss_precache = []
wss_work = []
wss_timeout = []
hash_to_precache = []
work_tracker = {}
blacklist = {}

worker_counter = 0

update_lock = 0

rethinkdb.set_loop_type("tornado")
connection = rethinkdb.connect("localhost", 28015)

timezone = rethinkdb.make_timezone('+00:00')

def print_time_debug(message):
    if args.verbose:
        print("(DEBUG)", end=' ')
        print_time(message)


def print_time(message):
    print(time.strftime("%d/%m/%Y %H:%M:%S") + " " + str(message))


def print_lists(work=False, demand=False, precache=False, timeout=False):
    if not (work or demand or precache):
        return
    s = "State of lists:"
    if work:
        s += "\n\t\t\twss_work: {}".format(wss_work)
    if demand:
        s += "\n\t\t\twss_demand: {}".format(wss_demand)
    if precache:
        s += "\n\t\t\twss_precache: {}".format(wss_precache)
    if timeout:
        s += "\n\t\t\twss_timeout: {}".format(wss_timeout)
    print_time(s)


def remove_from_timeout(client):
    print_time("Removing {} from timeout".format(client))
    if client in wss_timeout:
        wss_timeout.remove(client)


def get_all_clients():
    return set( wss_demand + wss_precache )


def build_blacklist(filepath='blacklist.txt'):
    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()
    except:
        print('Blacklist file does not exist: {}'.format(filepath))
        return {}

    blacklist = { line.split('\n')[0] for line in lines }
    print('Built blacklist:')
    { print('\t{}'.format(acc)) for acc in blacklist }

    return blacklist


# TODO we would like to not have to use this at all, once all services provide the account
def get_account_from_hash(hash_hex):
    """Returns tuple (account,is_open_block)"""

    get_account = '{ "action" : "block_account", "hash" : "%s"}' % hash_hex
    print_time_debug("action block_account request:\n{}".format(get_account))
    r = requests.post(rai_node_address, data=get_account)
    print_time_debug("action block_account response:\n{}".format(r.text))

    resulting_data = r.json()
    if 'account' in resulting_data:
        account = resulting_data['account'].replace('nano_','xrb_')
        return (account, False)
    elif 'error' in resulting_data:
        if resulting_data['error'] == 'Block not found':
            # This maybe a public key for an Open Block, convert to xrb address
            print_time("Open Block")
            account = nano.hex_to_account(hash_hex)
            print_time("The account is {}".format(account))
            return (account, True)
        else:  # other errors, for instance invalid hash
            return ('Error', None)
    else:
        return ('Error', None)


class WorkState(Enum):
    needs = "0"
    doing = "1"


class Work(tornado.web.RequestHandler):
    def data_received(self, chunk):
        pass

    @gen.coroutine
    def ws_demand(self, message):
        print_time("Sending via WS Demand")
        # randomise our wss list then cycle through until we manage to send a message
        random.shuffle(wss_demand)
        print_lists(work=1, demand=1, timeout=1)

        for ws in wss_demand:
            if not ws.ws_connection.stream.socket:
                print_time("Web socket does not exist anymore!!!")
                remove_from_lists(ws, timeout=True)
            else:
                if ws not in wss_work and ws not in wss_timeout:
                    ws.write_message(message)
                    wss_work.append(ws)
                    return ws

        # no clients
        return None

    @gen.coroutine
    def get_work_via_ws(self, hash_hex, account, new_entry=False):
        conn = yield connection

        # Get appropriate threshold value
        # TODO after prioritization PoW is implemented, calculate here an appropriate multiplier
        multiplier = 1.0
        threshold = nano.from_multiplier(nano.NANO_DIFFICULTY, multiplier)
        threshold_str = nano.threshold_to_str(threshold)

        if new_entry:
            # Insert new entry to be updated when work is returned by client
            yield rethinkdb.db("pow").table("hashes").insert(
                {"account": account, "hash": hash_hex, "work": WorkState.doing.value, "threshold": threshold_str}).run(conn)

        # Try up to 2 times, sending work to another client if the first fails
        error = None
        max_tries = 2
        try_count = 0
        while try_count < max_tries:
            try_count += 1

            # Send request to websocket clients to process
            send_result = yield self.ws_demand('{"hash" : "%s", "type" : "urgent", "threshold" : "%s"}' % (hash_hex,threshold_str))

            # If no clients, mark this account in DB
            if not send_result:
                print_time("No clients to to work for account %s" % account)
                if wss_timeout and try_count != max_tries:
                    print_lists(timeout=1)
                    removed = wss_timeout.pop(0)
                    print_time("Trying again after removing the oldest client in wss_timeout: {}".format(removed))
                    continue
                else:
                    error = 'no_clients'
                    break

            # Place this hash in the work tracker so it can be updated in the client handler
            client_id = None
            work_tracker[hash_hex] = -1
            t_start = time.time()
            print_time("Waiting for work...")
            while time.time() - t_start < TIMEOUT_ON_DEMAND:
                try:
                    if work_tracker.get(hash_hex) != -1:
                        work_output, client_id = work_tracker.pop(hash_hex)
                        print_time_debug("Hash handled in {} seconds: {}".format(time.time()-t_start, hash_hex))
                        raise gen.Return((work_output, client_id, multiplier))
                except KeyError:
                    print_time_debug("Hash was removed from work_tracker due to an error: {}".format(hash_hex))
                    error = 'failed_clients'
                    break
                else:
                    yield gen.sleep(0.01)

            # Took too long, add to timeout list
            client_to_timeout = send_result
            print_time("Placing {} in timeout for 3 minutes".format(client_to_timeout))
            wss_timeout.append(client_to_timeout)
            tornado.ioloop.IOLoop.current().add_timeout(time.time() + 3*60, lambda: remove_from_timeout(client_to_timeout))

        if error:
            # Mark account as needing work
            # Try updating, in case the account already exists in the DB
            changes = yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['account'] == account).update(
                    {"hash": hash_hex, "work": WorkState.needs.value, "threshold": threshold_str}).run(conn)
            if not changes or changes['unchanged']:
                # insert as new account
                yield rethinkdb.db("pow").table("hashes").insert(
                    {"account": account, "hash": hash_hex, "work": WorkState.needs.value, "threshold": threshold_str}).run(conn)

            if error == 'no_clients':
                raise gen.Return(('no clients', None, None))

            elif error == 'failed_clients':
                raise gen.Return(('error', None, None))

        else:
            # No specific error by client handler, simply a timeout reached
            raise gen.Return(('timeout', None, None))


    @gen.coroutine
    def post(self):
        receive_time = datetime.datetime.now(timezone)
        post_data = json.loads(self.request.body.decode('utf-8'))
        if 'hash' in post_data:
            hash_hex = post_data['hash'].upper()
        else:
            return_json = '{"status" : "no hash"}'
            print_time(return_json)
            self.write(return_json)
            return

        conn = yield connection

        # 1 Check key is valid
        if 'key' in post_data:
            print_time("found API key")
            key = post_data['key']
            key_hashed = hashlib.sha512(key.encode('utf-8')).hexdigest()
            service_data = yield rethinkdb.db("pow").table("api_keys").filter({"api_key": key_hashed}).nth(0).default(False).run(conn)
            if not service_data:
                print_time("incorrect API key")
                return_json = '{"status" : "incorrect key"}'
                print_time(return_json)
                self.write(return_json)
                return
            else:
                print_time("Correct API key from %s - continue" % service_data['username'])
        else:
            print_time("no API key")
            return_json = '{"status" : "no key"}'
            print_time(return_json)
            self.write(return_json)
            return

        # 2 Do they provide the account besides the hash?
        account = post_data.get('account') or None
        if account:
            print_time('Account provided: {}'.format(account))
            account = account.replace('nano_','xrb_')
            if account.find('xrb_') == -1:
                print_time_debug('NOTIFY SERVICE! Invalid account provided {}'.format(account))
                account = None
        else:
            print_time_debug('NOTIFY SERVICE! Ask to provide accounts')

            # Get account to setup DB entries and check if invalid hash
            account, is_open_block = get_account_from_hash(hash_hex)
            if account == 'Error':
                return_json = '{"status" : "bad hash"}'
                print_time(return_json)
                self.write(return_json)
                return

        # Update entry
        new_count = int(service_data['count']) + 1
        yield rethinkdb.db("pow").table("api_keys").filter(rethinkdb.row['api_key'] == key_hashed).update(
            {"count": new_count, "provides_accounts": post_data.get('account')!=None}).run(conn)

        # 3 Do we have hash in db? If not (or work is not provided) handle on demand
        hash_data = yield rethinkdb.db("pow").table("hashes").filter({"hash": hash_hex}).nth(0).default(False).run(conn)
        if hash_data:
            print_time('Found cached work value %s' % hash_data['work'])
            work_output = hash_data['work']
            if work_output == WorkState.needs.value or work_output == WorkState.doing.value:
                print_time("Empty work, get new on demand")
                work_output, client_id, multiplier = yield self.get_work_via_ws(hash_hex, account, new_entry=False)
                work_type = 'O'
            else:
                client_id = hash_data.get('last_worker') or None
                if 'threshold' in hash_data:
                    multiplier = nano.to_multiplier(nano.NANO_DIFFICULTY, nano.threshold_from_str(hash_data['threshold']))
                else:
                    multiplier = 1.0
                work_type = 'P'
        else:
            print_time('Not in DB, getting on demand...')
            work_output, client_id, multiplier = yield self.get_work_via_ws(hash_hex, account, new_entry=True)
            work_type = 'O'


        complete_time = datetime.datetime.now(timezone)

        # 4 Return work
        if work_output in ['timeout', 'no clients', 'error']:
            return_json = '{"status" : "%s"}' % work_output
            work_ok = False
        else:
            return_json = '{"work" : "%s"}' % work_output
            work_ok = True

        print_time(return_json)
        self.write(return_json)

        # Interface update
        if work_ok and interface:

            # Random request id
            request_id = str(uuid.UUID(int=random.SystemRandom().getrandbits(128)))

            # Send to interface
            service_id = service_data['id']
            interface.pow_update(request_id, service_id, client_id, work_type, multiplier, receive_time, complete_time)


class WSHandler(tornado.websocket.WebSocketHandler):

    worker_counter = 0

    def __init__(self, *args, **kwargs):
        WSHandler.worker_counter += 1
        self.id = WSHandler.worker_counter
        self.type = ''
        self.address = ''
        self.ip = ''
        super().__init__(*args, **kwargs)

    def __repr__(self):
        return '{id} ({state})'.format(id=self.id or '?',
                                       state='timeout' if self in wss_timeout else 'busy' if self in wss_work else 'free')

    @staticmethod
    def validate_work(hash_hex, work, threshold):
        get_validation = '{ "action" : "work_validate", "hash" : "%s", "work": "%s", "threshold": "%s" }' % (hash_hex, work, threshold)
        r = requests.post(rai_node_address, data=get_validation)
        resulting_validation = r.json()
        if 'error' in resulting_validation:
            print_time_debug(get_validation)
            raise gen.Return('Error in validation: {}'.format(resulting_validation))
        return int(resulting_validation['valid'])

    def open(self):
        print_time('New worker connected - {}'.format(self.id))
        self.ip = self.request.remote_ip
        print_time('IP: {}'.format(self.ip))

    @gen.coroutine
    def on_message(self, message):
        print_time('Message from worker {}: {}'.format(self.id, message))
        try:
            ws_data = json.loads(message)
            if 'address' not in ws_data:
                raise Exception('Incorrect data from client: {}'.format(ws_data))

            self.address = ws_data['address']

            if 'work_type' in ws_data:
                # Setup message handling

                # remove from any lists
                self.remove_from_lists()

                # restrict clients per IP
                connected_clients = get_all_clients()
                same_ip = list(filter(lambda c: c.ip == self.ip, connected_clients))
                if len(same_ip) >= 2:  # this client is not yet in the lists
                    print_time("Client attempted to connect more than 2 clients: IP: {} , already in list: {} , new client: {}".format(self.ip, same_ip, self))
                    self.write_message('{"status": "error", "description": "Maximum of 2 clients allowed"}')
                    self.close()
                    return

                # handle setup message for work type
                if ws_data['address'] in blacklist:
                    print("Blacklisted: {}".format(ws_data['address']))
                else:
                    work_type = ws_data['work_type']
                    print_time("Found work_type -> {}".format(work_type))
                    try:
                        self.update_work_type(work_type)
                    #self.write_message('{"status": "success"}')
                    except Exception as e:
                        print_time(e)
                    #self.write_message('{"status": "error", "description": "%s"}' % e)
            else:
                if 'hash' not in ws_data or 'work' not in ws_data:
                    raise Exception('Incorrect data from client: {}'.format(ws_data))

                # handle work message
                hash_hex = ws_data['hash'].upper()
                work = ws_data['work']
                payout_account = ws_data['address'].lower()

                if work == 'error':
                    raise Exception("'Something wrong with the client, work returned as error'")

                # check the threshold at which this was computed
                # defaults to NANO_DIFFICULTY if not found (e.g. early entries in DB)
                conn = yield connection
                data = yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['hash'] == hash_hex).nth(0).default(False).run(conn)
                if data:
                    threshold_str = data.get("threshold") or nano.threshold_to_str(nano.NANO_DIFFICULTY)
                else:
                    threshold_str = nano.threshold_to_str(nano.NANO_DIFFICULTY)
                print_time_debug("Validating hash {},  work {}, threshold {}".format(hash_hex, work, threshold_str))

                # validate the work from client
                valid = self.validate_work(hash_hex, work, threshold_str)
                if valid:
                    yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['hash'] == hash_hex).update(
                        {"work": work, "last_worker": payout_account}).run(conn)
                    if hash_hex in hash_to_precache:
                        hash_to_precache.remove(hash_hex)

                    # If it was in work_tracker, then it's urgent work, else precache
                    if work_tracker.get(hash_hex) != None:
                        update_count_type = 'urgent'

                        # Update the work tracker so that the service wait loop knows it is done
                        work_tracker[hash_hex] = (work, payout_account)
                    else:
                        update_count_type = 'precache'

                    # Add work record to client database to allow payouts
                    clients_data = yield rethinkdb.db("pow").table("clients").filter(
                        rethinkdb.row['account'] == payout_account).nth(0).default(False).run(conn)
                    if clients_data:
                        client = clients_data
                        count = int(client['count'])
                        count_type = int(client['{}_count'.format(update_count_type)])
                        yield rethinkdb.db("pow").table("clients").filter(
                            rethinkdb.row['account'] == payout_account).update(
                                {"count": count + 1, "{}_count".format(update_count_type): count_type + 1, "time": time.time()}).run(conn)
                    else:
                        yield rethinkdb.db("pow").table("clients").insert(
                            {"account": payout_account, "count": 1, "{}_count".format(update_count_type): 1, "time": time.time()}).run(conn)

                    # Remove from work list
                    if self in wss_work:
                        print_time("Removing {} from wss_work".format(self))
                        wss_work.remove(self)

                else:
                    raise Exception("Failed to validate work - {} for worker {}".format(valid, self))

        except Exception as e:
            print_time("Error {}".format(e))
            if self in wss_work:
                print_time("Removing {} from wss_work after exception".format(self))
                wss_work.remove(self)

            # Remove from work tracker, the service wait loop can decide what to do (for instance give work to someone else)
            try:
                work_tracker.pop(hash_hex)
            except KeyError:
                print_time("Error - tried to remove hash but it was no longer in work_tracker: {}".format(hash_hex))
            except UnboundLocalError:  # there was no hash
                pass

    def update_work_type(self, work_type):

        if work_type == 'any':
            # Add to both demand and precache
            wss_demand.append(self)
            wss_precache.append(self)
            self.type = 'B'
        elif work_type == 'precache_only':
            # Add to precache
            wss_precache.append(self)
            self.type = 'P'
        elif work_type == 'urgent_only':
            # Add to demand
            wss_demand.append(self)
            self.type = 'O'
        else:
            raise Exception('Invalid work type {}'.format(work_type))

    def on_close(self):
        print_time('Worker disconnected - {}'.format(self.id))
        self.remove_from_lists()

    def remove_from_lists(self, timeout=False):
        for l in [wss_work, wss_demand, wss_precache]:
            if self in l:
                l.remove(self)
        if timeout:
            if self in wss_timeout:
                wss_timeout.remove(self)

    def check_origin(self, origin):
        return True


application = tornado.web.Application([
    (r'/group/', WSHandler),
    (r"/work", Work),
])


@gen.coroutine
def push_precache():
  global update_lock
  print("Global update_lock: {}".format(update_lock))
  if update_lock == 0:
    print("\nPre Hash to precache: {} {}".format(len(hash_to_precache), hash_to_precache))
    hash_count = 0
    work_count = 0

    # Get appropriate threshold value
    # TODO what is a good threshold value for precaching?
    multiplier = 1.0
    threshold = nano.from_multiplier(nano.NANO_DIFFICULTY, multiplier)
    threshold_str = nano.threshold_to_str(threshold)

    if hash_to_precache:
        print_time("Got precache work to push")
        random.shuffle(wss_precache)
        print_lists(work=1, precache=1)
        # if all precachers are doing work... no free workers
        if set(wss_precache).issubset(set(wss_work)):
            print_time("No clients available to push precache")
            return


    for hash_hex in hash_to_precache:
        hash_count = hash_count + 1
        hash_handled = False
        for work_clients in wss_precache:  # type: WSHandler
            print_time("Sending via WS Precache")
            print_lists(work=1, precache=1)
            try:
                if not work_clients.ws_connection.stream.socket:
                    print_time("Web socket does not exist anymore!!!")
                    wss_precache.remove(work_clients)
                    wss_work.remove(work_clients)
                else:
                    if work_clients not in wss_work:
                        work_count = work_count + 1
                        message = '{"hash" : "%s", "type" : "precache", "threshold" : "%s"}' % (hash_hex, threshold_str)
                        work_clients.write_message(message)
                        wss_work.append(work_clients)
                        print_time_debug(message)
                        try:
                            hash_to_precache.remove(hash_hex)
                            hash_handled = True
                        except Exception as e:
                            print_time("Failed to remove hash from precache list: {}".format(e))
                            pass
                        break

            except Exception as e:
                print_time('Error when sending via WS precache: {}'.format(e))
                if work_clients in wss_precache:
                    wss_precache.remove(work_clients)
                    print_time('Client {} removed from precache work'.format(work_clients.id))
                if work_clients in wss_work:
                    wss_work.remove(work_clients)

        if hash_handled:
            print_time("Hash was given to a precache client")
        else:
            print_time("Precache not handled - no free workers?")
            break
    print("\nPost1 Hash to precache: {} {}".format(len(hash_to_precache), hash_to_precache))
  else:
    print("Update - busy")

@gen.coroutine
def precache_update():
  global update_lock
  if update_lock == 0:
    update_lock = 1
    count_updates = 0
    work_count = 0
    up_to_date = 0
    not_in_queue = 0
    not_up_to_data = 0
    delete_error = 0
    conn = yield connection
    #   print_time("precache_update")
    precache_data = yield rethinkdb.db("pow").table("hashes").run(conn)
    while (yield precache_data.fetch_next()):
        try:
            user = yield precache_data.next()
            count_updates = count_updates + 1
            print("Count Updates: {}".format(count_updates))
            if user['work'] == WorkState.doing.value:
                # Reset work as taken too long
                work_count = work_count + 1
                #             print_time("%s : Request too long, reset" % user['account'])
                yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['account'] == user['account']).update(
                    {"work": WorkState.needs.value}).run(conn)
                if user['hash'] not in hash_to_precache:
                    hash_to_precache.append(user['hash'])
                continue

            get_frontier = '{ "action" : "account_info", "account" : "%s" }' % user['account']
            r = requests.post(rai_node_address, data=get_frontier)
            results = r.json()
            if 'frontier' in results:
                if results['frontier'] == user['hash']:
                    up_to_date = up_to_date + 1
                    if user['work'] == WorkState.needs.value:
                        if user['hash'] not in hash_to_precache:
                            not_in_queue = not_in_queue + 1
                            hash_to_precache.append(user['hash'])
                else:
                    not_up_to_data = not_up_to_data + 1
                    if user['hash'] not in hash_to_precache:
                        hash_to_precache.append(user['hash'])
                    yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['account'] == user['account']).update(
                        {"work": WorkState.needs.value, "hash": results['frontier']}).run(conn)
            else:
                # TODO once all services provide account, this cant be an open block, so simply delete entry from DB
                print_time('Checking to see if it is a case of a mistaken open block')
                print_time_debug(get_frontier)
                print_time_debug(results)

                # In this error, perhaps the system mistakenly added as an open block, when the node simply didn't have that block yet.
                # in that case, the next RPC will return a valid account now, and not error
                account, is_open_block = get_account_from_hash(user['hash'])
                if account != 'Error' and not is_open_block:
                    print_time('Hash now corresponds to an account, deleting last entry and setting up another for precache')
                    print_time("Deleting %s" % user['id'])
                    yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['id'] == user['id']).delete().run(conn)

                    # Add to precache

                    # Get appropriate threshold value
                    # TODO what is a good threshold value for precaching?
                    multiplier = 1.0
                    threshold = nano.from_multiplier(nano.NANO_DIFFICULTY, multiplier)
                    threshold_str = nano.threshold_to_str(threshold)

                    yield rethinkdb.db("pow").table("hashes").insert(
                        {"account": account, "hash": user['hash'], "work": WorkState.needs.value, "threshold": threshold_str}).run(conn)

                    hash_to_precache.append(user['hash'])

                else:  # 'error' or otherwise:
                    print_time('Still no valid account, deleting entry completely from DB')
                    delete_error = delete_error + 1
                    print_time("Deleting %s" % user['id'])
                    yield rethinkdb.db("pow").table("hashes").filter(rethinkdb.row['id'] == user['id']).delete().run(conn)

        except Exception as e:
            print_time_debug(e)

    print_time("Count: {:d}, Work: {:d}, Up to date:  {:d}, Not in queue: {:d}, Not up to date: {:d}, Delete error: {:d}".format(
                count_updates, work_count, up_to_date, not_in_queue, not_up_to_data, delete_error))
    print_lists(work=1, precache=1, demand=1, timeout=1)
    update_lock = 0
  else:
    print("Can't update as locked")

@gen.coroutine
def setup_db():
    global update_lock
    update_lock = 0

    print_time("Update DB")
    conn = yield connection
    data = yield rethinkdb.db("pow").table("hashes").run(conn)
    while (yield data.fetch_next()):
        documents = yield data.next()
        if documents['work'] == WorkState.needs.value:
            if documents['hash'] not in hash_to_precache:
                hash_to_precache.append(documents['hash'])


@gen.coroutine
def update_interface_clients():
    conn = yield connection
    connected_clients = get_all_clients()
    seen = set()
    unique_clients_by_account = [c for c in connected_clients if c.address not in seen and not seen.add(c.address)]
    accounts = [c.address for c in unique_clients_by_account]
    data = yield rethinkdb.db("pow").table("clients").filter(rethinkdb.row["account"] in accounts).run(conn)

    counts_by_type = dict()
    for client_data in data.items:
        counts_by_type[client_data["account"]] = {"precache": client_data["precache_count"], "urgent": client_data["urgent_count"]}

    clients = list(map(lambda c: {
        'client_id': c.address+'_'+c.type,
        'client_address': c.address,
        'client_type': c.type,
        'client_demand_count': counts_by_type[c.address]["urgent"],
        'client_precache_count': counts_by_type[c.address]["precache"]
    }, unique_clients_by_account))

    interface.clients_update(clients)
    print_time_debug("Updated interface clients")

@gen.coroutine
def update_interface_services():
    conn = yield connection
    registered_services = yield rethinkdb.db("pow").table("api_keys").run(conn)
    if registered_services:
        services = list(map(lambda s: {
            'service_id': s.get('id'),
            'service_name': (s.get('display_name') or s.get('username')) if s.get('public') else None,
            'service_web': (s.get('website') or '') if s.get('public') else ''
        }, registered_services.items))

        interface.services_update(services)
        print_time_debug("Updated interface services")
    else:
        print_time_debug("Could not update interface services")


if __name__ == "__main__":
    http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(int(args.internal_port))
    myIP = socket.gethostbyname(socket.gethostname())

    # setup interface
    if args.interface:
        interface = InterfaceClient()
        if not interface.read_config('interface.cfg'):
            print_time('INTERFACE DISABLED')
            interface = None
        else:
            print_time('Interface is setup -> {}'.format(interface.server))
    else:
        interface = None

    tornado.ioloop.IOLoop.current().run_sync(setup_db)

    blacklist = build_blacklist('blacklist.txt')

    print_time('*** Websocket Server Started at %s***' % myIP)
    pc = tornado.ioloop.PeriodicCallback(precache_update, 300000)
    pc.start()
    push = tornado.ioloop.PeriodicCallback(push_precache, 5000)
    push.start()
    if interface:
        # clients update every 10 seconds
        icli = tornado.ioloop.PeriodicCallback(update_interface_clients, 10000)
        icli.start()

        # services update every 60 seconds
        iserv = tornado.ioloop.PeriodicCallback(update_interface_services, 60000)
        iserv.start()

    main_loop = tornado.ioloop.IOLoop.instance()

    try:
        main_loop.start()
    except KeyboardInterrupt:
        pass
