from collections import defaultdict
import logging
import time

from data import *
from net import IOThread

# Constants
DEFAULT_STABILIZE_INTERVAL = 10
QUERY_TIMEOUT = 10

## Exceptions
class QueryFailed(IOError):
    pass

class UnexpectedMessage(IOError):
    pass

class InterruptedQuery(RuntimeError):
    pass

class BadQueryCallbackResult(TypeError):
    pass

# pointer -> (msg -> query or [msg], state) -> query
def ping(node, cb):
    return Query(node, Message("ping"), "pong", cb)

def get_succ_list(node, cb):
    return Query(node, Message("get_succ_list"), "got_succ_list", cb)

def get_pred_and_succs(node, cb):
    return Query(node, Message("get_pred_and_succs"), "got_pred_and_succs", cb)

def get_best_predecessor(node, id, cb):
    return Query(node, Message("get_best_predecessor", [id]), "got_best_predecessor", cb)

def notify(node):
    return [(node, Message("notify"))]

# pointer -> pointer -> query
def rectify_query(pred, notifier):
    def cb(state, pong):
        if pong is None or between(state.pred.id, notifier.id, state.ptr.id):
            return None, state._replace(pred=notifier)
        else:
            return None, state
    return ping(pred, cb)

# pointer -> query
def stabilize_query(succ):
    def cb(state, msg):
        assert succ == state.succ_list[0]
        if msg is not None:
            pred_and_succ_list = msg.data
            new_succ, succs = pred_and_succ_list[0], pred_and_succ_list[1:]
            state = state._replace(succ_list=make_succs(succ, succs))
            if between(state.ptr.id, new_succ.id, succ.id):
                return stabilize2(new_succ), state
            else:
                return notify(succ), state
        else:
            rest = state.succ_list[1:]
            return stabilize_query(rest[0]), state._replace(succ_list=rest)
    return get_pred_and_succs(succ, cb)

# pointer -> query
def stabilize2(new_succ):
    def cb(state, msg):
        if msg is not None:
            succs = make_succs(new_succ, msg.data)
            return notify(new_succ), state._replace(succ_list=succs)
        else:
            return notify(state.succ_list[0]), state
    return get_succ_list(new_succ, cb)

# pointer -> id -> query
def join_query(known, me):
    def cb(state, msg):
        if msg is not None:
            new_succ = msg.data[0]
            return join2(new_succ), state
        else:
            return None, state
    return lookup_succ(known, me, cb)

# pointer -> id -> callback -> query
def lookup_succ(node, id, cb):
    def inner_cb(state, msg):
        if msg is not None:
            return get_succ(msg.data[0], cb), state
        else:
            return cb(state, msg)
    return lookup_predecessor(node, id, inner_cb)

# pointer -> callback -> query
def get_succ(node, cb):
    def inner_cb(state, msg):
        if msg is not None:
            return cb(state, msg.data[0])
        else:
            return cb(state, msg)
    return get_succ_list(node, cb)

# pointer -> id -> callback -> query
def lookup_predecessor(node, id, cb):
    def inner_cb(state, msg):
        if msg is not None:
            best_pred = msg.data[0]
            if best_pred == node:
                # it's the best predecessor
                return cb(state, msg)
            else:
                # it's referring us to a better one
                return lookup_predecessor(best_pred, id, cb), state
        else:
            return cb(state, msg)
    return get_best_predecessor(node, id, inner_cb)

# state -> pointer -> query
def join2(new_succ):
    def cb(state, msg):
        if msg is not None:
            succs = make_succs(new_succ, msg.data)
            return None, state._replace(succ_list=succs, pred=None, joined=True)
        else:
            return None, state
    return get_succ_list(new_succ, cb)


class Node(object):
    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        for i, new_val in enumerate(new_state):
            if not hasattr(self, "_state") or self._state[i] != new_val:
                name = new_state._fields[i]
                val = new_state[i]
                if "query" in name:
                    continue
                if isinstance(val, list) and len(val) > 0:
                    if isinstance(val[0], Pointer):
                        val = "[{}]".format(", ".join(str(p.id) for p in val))
                elif isinstance(val, Pointer):
                    val = val.id
                self.logger.info("{} := {}".format(name, str(val)))
        self._state = new_state

    def __init__(self, ip, pred=None, succ_list=None,
            stabilize_interval=DEFAULT_STABILIZE_INTERVAL):
        ptr = Pointer(ip)
        self.logger = logging.getLogger(__name__ + "({})".format(ptr.id))
        self.stabilize_interval = stabilize_interval
        state = State(ptr=ptr, pred=pred, succ_list=[], joined=False,
                rectify_with=None, known=None, query=None, query_sent=None)

        if succ_list is None:
            if pred is not None:
                raise ValueError("provided pred but not succ_list")
        elif len(succ_list) == SUCC_LIST_LEN:
            if pred is None:
                raise ValueError("provided succ_list but not pred")
            state = state._replace(joined=True, succ_list=succ_list)
        else:
            raise ValueError("succ_list isn't the right length")

        self.state = state

        self.io = IOThread(ip)
        self.started = False

        # map from ids to the clients that have asked for the id's successor
        self.lookup_clients = defaultdict(list)

    def start(self, known=None):
        if self.started:
            raise RuntimeError("already started")
        self.started = True
        self.io.start()
        sends, self.state = self.start_handler(self.state, known)
        self.send_all(sends)
        self.main_loop()

    # can only be run once we've joined and stabilized
    def main_loop(self):
        while True:
            if time.time() - self.last_stabilize > self.stabilize_interval:
                outs, self.state = self.timeout_handler(self.state)
                self.send_all(outs)
            res = self.io.recv()
            if res is not None:
                src, msg = res
                sends, self.state = self.recv_handler(self.state, src, msg)
                self.send_all(sends)

    def send_all(self, sends):
        for dst, msg in sends:
            self.io.send(dst, msg)

    # state -> msg -> [pointer * message] * state
    def end_query(self, state, msg):
        # log timing data for calibrating timeouts
        duration = time.time() - state.query_sent
        dst = state.query.dst
        if msg is not None:
            self.logger.debug("query to {} completed in {} seconds".format(dst, duration))
        else:
            self.logger.debug("query to {} failed after {} seconds".format(dst, duration))

        output, state = state.query.cb(state, msg)
        state = state._replace(query=None)
        outs = []
        if output is None:
            if state.joined:
                outs, state = self.try_rectify(state)
        elif isinstance(output, Query):
            outs, state = self.start_query(state, output)
        elif isinstance(output, list):
            if state.joined:
                rectify_sends, state = self.try_rectify(state)
                outs = output + rectify_sends
            else:
                outs = output
        else:
            raise BadQueryCallbackResult(output)
        return outs, state

    # state -> [pointer * message] * state
    def try_rectify(self, state):
        if state.rectify_with is None:
            return [], state
        if state.query is not None:
            raise InterruptedQuery(state.query)
        if state.pred is None:
            state = state._replace(pred=state.rectify_with)
            return [], state
        else:
            new_succ = state.rectify_with
            state = state._replace(rectify_with=None)
            return self.start_query(state, rectify_query(state.pred, new_succ))

    # state, query -> [ptr * msg] * state
    def start_query(self, state, query):
        self.logger.debug(repr(query))
        if state.query is not None:
            raise InterruptedQuery(state.query)
        state = state._replace(query=query, query_sent=time.time())
        return [(query.dst, query.msg)], state

    # state -> pointer -> message -> [pointer * message] * state
    def recv_handler(self, state, src, msg):
        kind = msg.kind
        outs = []
        if kind == "get_best_predecessor":
            id = msg.data[0]
            pred = best_predecessor(state, id)
            outs = [(src, Message("got_best_predecessor", [pred]))]
        elif kind == "get_succ_list":
            outs = [(src, Message("got_succ_list", state.succ_list))]
        elif kind == "get_pred_and_succs":
            pred_and_succs = [state.pred] + state.succ_list
            msg = Message("got_pred_and_succs", pred_and_succs)
            outs = [(src, msg)]
        elif kind == "notify":
            state = state._replace(rectify_with=src)
            if state.query is None:
                outs, state = self.try_rectify(state)
        elif kind == "ping":
            outs = [(src, Message("pong"))]
        elif state.query is not None and kind == state.query.res_kind and src == state.query.dst:
            outs, state = self.end_query(state, msg)
        else:
            # if this comes up it's likely because queries are slow and we got a spurious timeout
            raise UnexpectedMessage(msg)
        return outs, state

    # state -> [pointer * message] * state
    def timeout_handler(self, state):
        msgs = []
        if state.query is None:
            if state.joined:
                self.last_stabilize = time.time()
                msgs, state = self.start_query(state, stabilize_query(state.succ_list[0]))
            if not state.joined:
                msgs, state = self.start_query(state, join_query(state.known, state.ptr.id))
        elif time.time() - state.query_sent > QUERY_TIMEOUT:
            msgs, state = self.end_query(state, None)
        else:
            msgs = []
        return msgs, state

    # state -> ptr -> [ptr * msg] * state
    # or state -> NoneType -> [ptr * msg] * state, since you can give this a fully initialized state
    # to skip join stuff
    def start_handler(self, state, known):
        if len(state.succ_list) == 0:
            if known is None:
                raise ValueError("can't join without a known node!")
            state = state._replace(known=known)
            self.last_stabilize = time.time() - self.stabilize_interval
            return self.start_query(state, join_query(state.known, state.ptr.id))
        else:
            # fake it
            self.last_stabilize = time.time()
            return [], state
