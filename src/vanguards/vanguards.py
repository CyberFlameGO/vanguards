#!/usr/bin/env python

import getpass
import sys
import logging
import copy
import random
import os
import time
import functools
import argparse
import pickle

import stem
import stem.connection
import stem.descriptor
from stem.control import Controller

from .NodeSelection import BwWeightedGenerator, NodeRestrictionList
from .NodeSelection import FlagsRestriction
from .logger import plog
from .bandguards import BandwidthStats

try:
  xrange
except NameError:
  xrange = range


NUM_LAYER1_GUARDS = 2 # 0 is Tor default
NUM_LAYER2_GUARDS = 4
NUM_LAYER3_GUARDS = 8

# In days:
LAYER1_LIFETIME = 0 # Use tor default

# In hours
MIN_LAYER2_LIFETIME = 24*1
MAX_LAYER2_LIFETIME = 24*45

# In hours
MIN_LAYER3_LIFETIME = 1
MAX_LAYER3_LIFETIME = 48

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 9051
CONTROL_SOCKET = None

SEC_PER_HOUR = (60*60)


# Use count limits. These limits control when we emit warnings about circuits
#
# Minimum number of hops we have to see before applying use stat checks
USE_COUNT_TOTAL_MIN = 100

# Number of hops to scale counts down by two at
USE_COUNT_SCALE_AT = 1000

# Minimum number of times a relay has to be used before we check it for
# overuse
USE_COUNT_RELAY_MIN = 10

# How many times more than its bandwidth must a relay be used?
USE_COUNT_RATIO = 2.0


def get_rlist_and_rdict(controller):
  sorted_r = list(controller.get_network_statuses())
  dict_r = {}
  # Let's not use unmeasured relays
  for r in sorted_r:
    if r.measured == None:
      r.measured = 0
  sorted_r.sort(key = lambda x: x.measured, reverse = True)

  for i in xrange(len(sorted_r)): sorted_r[i].list_rank = i

  for r in sorted_r: dict_r[r.fingerprint] = r

  return (sorted_r, dict_r)

def connect():
  if CONTROL_SOCKET != None:
    try:
      controller = Controller.from_socket_file(CONTROL_SOCKET)
    except stem.SocketError as exc:
      print("Unable to connect to Tor Control Socket at "+CONTROL_SOCKET+": %s" % exc)
      sys.exit(1)
  else:
    try:
      controller = Controller.from_port(CONTROL_HOST, CONTROL_PORT)
    except stem.SocketError as exc:
      print("Unable to connect to Tor Control Port at "+CONTROL_HOST+":"
             +str(CONTROL_PORT)+" %s" % exc)
      sys.exit(1)

  try:
    controller.authenticate()
  except stem.connection.MissingPassword:
    pw = getpass.getpass("Controller password: ")

    try:
      controller.authenticate(password = pw)
    except stem.connection.PasswordAuthFailed:
      print("Unable to authenticate, password is incorrect")
      sys.exit(1)
  except stem.connection.AuthenticationFailure as exc:
    print("Unable to authenticate: %s" % exc)
    sys.exit(1)

  print("Tor is running version %s" % controller.get_version())

  return controller

def get_consensus_weights(controller):
  consensus = os.path.join(controller.get_conf("DataDirectory"),
                           "cached-microdesc-consensus")
  parsed_consensus = next(stem.descriptor.parse_file(consensus,
                          document_handler =
                            stem.descriptor.DocumentHandler.BARE_DOCUMENT))

  assert(parsed_consensus.is_consensus)
  return parsed_consensus.bandwidth_weights

def setup_options():
  global LAYER1_LIFETIME
  global MIN_LAYER2_LIFETIME, MAX_LAYER2_LIFETIME
  global MIN_LAYER3_LIFETIME, MAX_LAYER3_LIFETIME
  global NUM_LAYER1_GUARDS
  global NUM_LAYER2_GUARDS
  global NUM_LAYER3_GUARDS
  global CONTROL_HOST, CONTROL_PORT, CONTROL_SOCKET

  # XXX: Advanced vs simple options (--client, --service, etc)
  parser = argparse.ArgumentParser()
  parser.add_argument("--num_guards", type=int, dest="num_layer1",
                    help="Number of entry gurds (default "+str(NUM_LAYER1_GUARDS)+
                    "; 0 means use tor's value)", default=NUM_LAYER1_GUARDS)

  parser.add_argument("--num_mids", type=int, dest="num_layer2",
                    help="Number of Layer2 guards (default "+str(NUM_LAYER2_GUARDS)+
                    "; 0 disables)",
                    default=NUM_LAYER2_GUARDS)

  parser.add_argument("--num_ends", type=int, dest="num_layer3",
                    default=NUM_LAYER3_GUARDS,
                    help="Number of Layer3 guards (default "+str(NUM_LAYER3_GUARDS)+
                    "; 0 disables)")

  parser.add_argument("--guard_lifetime", type=int, dest="guard_lifetime",
                    default=LAYER1_LIFETIME,
                    help="Lifetime of Layer1 in days (default "+str(LAYER1_LIFETIME)+
                    "; 0 means use tor's value)")

  parser.add_argument("--mid_lifetime_min", type=int, dest="mid_lifetime_min",
                    default=MIN_LAYER2_LIFETIME,
                    help="Min lifetime of Layer2 in hours (default "+str(MIN_LAYER2_LIFETIME)+
                    ")")

  parser.add_argument("--mid_lifetime_max", type=int, dest="mid_lifetime_max",
                    default=MAX_LAYER2_LIFETIME,
                    help="Max lifetime of Layer2 in hours (default "+str(MAX_LAYER2_LIFETIME)+
                    ")")

  parser.add_argument("--end_lifetime_min", type=int, dest="end_lifetime_min",
                    default=MIN_LAYER3_LIFETIME,
                    help="Min lifetime of Layer3 in hours (default "+str(MIN_LAYER3_LIFETIME)+
                    ")")

  parser.add_argument("--end_lifetime_max", type=int, dest="end_lifetime_max",
                    default=MAX_LAYER3_LIFETIME,
                    help="Max lifetime of Layer3 in hours (default "+str(MAX_LAYER3_LIFETIME)+
                    ")")

  parser.add_argument("--state_file", dest="state_file", default="vanguards.state",
                    help="File to store vanguard state (default: DataDirectory/vanguards)")

  parser.add_argument("--control_host", dest="control_host", default=CONTROL_HOST,
                    help="The IP address of the Tor Control Port to connect to (default: "+
                    CONTROL_HOST+")")
  parser.add_argument("--control_port", type=int, dest="control_port",
                      default=CONTROL_PORT,
                      help="The Tor Control Port to connect to (default: "+
                      str(CONTROL_PORT)+")")

  parser.add_argument("--control_socket", dest="control_socket",
                      default=CONTROL_SOCKET,
                      help="The Tor Control Socket path to connect to "+
                      "(default: "+str(CONTROL_SOCKET)+")")

  options = parser.parse_args()

  (LAYER1_LIFETIME, MIN_LAYER2_LIFETIME, MAX_LAYER2_LIFETIME,
   MIN_LAYER3_LIFETIME, MAX_LAYER3_LIFETIME, NUM_LAYER1_GUARDS,
   NUM_LAYER2_GUARDS, NUM_LAYER3_GUARDS, CONTROL_HOST, CONTROL_PORT,
   CONTROL_SOCKET) = (options.guard_lifetime,
   options.mid_lifetime_min, options.mid_lifetime_max,
   options.end_lifetime_min, options.end_lifetime_max,
   options.num_layer1, options.num_layer2, options.num_layer3,
   options.control_host, options.control_port, options.control_socket)

  return options

class GuardNode:
  def __init__(self, idhex, chosen_at, expires_at):
    self.idhex = idhex
    self.chosen_at = chosen_at
    self.expires_at = expires_at

  def __str__(self):
    return self.idhex

  def __repr__(self):
    return self.idhex

class UseCount:
  def __init__(self, idhex, weight):
    self.idhex = idhex
    self.used = 0
    self.weight = weight

class RendGuard:
  def __init__(self):
    self.use_counts = {}
    self.total_use_counts = 0

  def get_service_rend_node(self, path):
    if NUM_LAYER3_GUARDS:
      return path[5]
    else:
      return path[4]

  def check_rend_use(self, purpose, path):
    if purpose != "HS_SERVICE_REND":
      return

    r = self.get_service_rend_node(path)

    if r not in self.use_counts:
      plog("NOTICE", "Relay "+r+" is not in our consensus, but someone is using it!")
      self.use_counts[r] = UseCount(r, 0)

    self.use_counts[r].used += 1
    self.total_use_counts += 1.0

    # TODO: Can we base this check on statistical confidence intervals?
    if self.total_use_counts > USE_COUNT_TOTAL_MIN and \
       self.use_counts[r].used >= USE_COUNT_RELAY_MIN:
      plog("INFO", "Relay "+r+" used "+str(self.use_counts[r].used)+
                  " times out of "+str(int(self.total_use_counts)))

      if self.use_counts[r].used/self.total_use_counts > \
         self.use_counts[r].weight*USE_COUNT_RATIO:
        plog("WARN", "Relay "+r+" used "+str(self.use_counts[r].used)+
                     " times out of "+str(int(self.total_use_counts))+
                     ". This is above its weight of "+
                     str(self.use_counts[r].weight))
        self.try_close_circuit(circ_id)

  def xfer_use_counts(self, node_gen):
    old_counts = self.use_counts
    self.use_counts = {}
    for r in node_gen.sorted_r:
       self.use_counts[r.fingerprint] = UseCount(r.fingerprint, 0)

    for i in xrange(len(node_gen.rstr_routers)):
      r = node_gen.rstr_routers[i]
      self.use_counts[r.fingerprint].weight = \
         node_gen.node_weights[i]/node_gen.weight_total

    # Periodically we divide counts by two, to avoid overcounting
    # high-uptime relays vs old ones
    for r in old_counts:
      if r not in self.use_counts: continue
      if self.total_use_counts > USE_COUNT_SCALE_AT:
        self.use_counts[r].used = old_counts[r].used/2
      else:
        self.use_counts[r].used = old_counts[r].used

    self.total_use_counts = sum(map(lambda x: self.use_counts[x].used,
                                    self.use_counts))
    self.total_use_counts = float(self.total_use_counts)

class VanguardState:
  def __init__(self):
    self.layer2 = []
    self.layer3 = []
    self.rendguard = RendGuard()
    self.controller = None

  def write_to_file(self, outfile):
    controller = self.controller
    self.controller = None
    ret = pickle.dump(self, outfile)
    self.controller = controller
    return ret

  @staticmethod
  def read_from_file(infile):
    return pickle.load(infile)

  def try_close_circuit(self, circ_id):
    try:
      self.controller.close_circuit(circ_id)
      plog("NOTICE", "We force-closed circuit "+str(circ_id))
    except stem.InvalidRequest as e:
      plog("INFO", "Failed to close circuit "+str(circ_id)+": "+str(e.message))

  # XXX: Log guards

  def layer2_guardset(self):
    return ",".join(map(lambda g: g.idhex, self.layer2))

  def layer3_guardset(self):
    return ",".join(map(lambda g: g.idhex, self.layer3))

  # Adds a new layer2 guard
  def add_new_layer2(self, generator):
    guard = next(generator)
    while guard.fingerprint in map(lambda g: g.idhex, self.layer2):
      guard = next(generator)

    now = time.time()
    expires = now + max(random.uniform(MIN_LAYER2_LIFETIME*SEC_PER_HOUR,
                                       MAX_LAYER2_LIFETIME*SEC_PER_HOUR),
                        random.uniform(MIN_LAYER2_LIFETIME*SEC_PER_HOUR,
                                       MAX_LAYER2_LIFETIME*SEC_PER_HOUR))
    self.layer2.append(GuardNode(guard.fingerprint, now, expires))

  def add_new_layer3(self, generator):
    guard = next(generator)
    while guard.fingerprint in map(lambda g: g.idhex, self.layer3):
      guard = next(generator)

    now = time.time()
    expires = now + max(random.uniform(MIN_LAYER3_LIFETIME*SEC_PER_HOUR,
                                       MAX_LAYER3_LIFETIME*SEC_PER_HOUR),
                        random.uniform(MIN_LAYER3_LIFETIME*SEC_PER_HOUR,
                                       MAX_LAYER3_LIFETIME*SEC_PER_HOUR))
    self.layer3.append(GuardNode(guard.fingerprint, now, expires))

  def _remove_expired(self, remove_from, now):
    for g in list(remove_from):
      if g.expires_at < now:
        remove_from.remove(g)

  def replace_expired(self, generator):
    plog("INFO", "Replacing any old vanguards. Current "+
                 " layer2 guards: "+self.layer2_guardset()+
                 " Current layer3 guards: "+self.layer3_guardset())

    now = time.time()

    self._remove_expired(self.layer2, now)
    self.layer2 = self.layer2[:NUM_LAYER2_GUARDS]
    self._remove_expired(self.layer3, now)
    self.layer3 = self.layer3[:NUM_LAYER2_GUARDS]

    while len(self.layer2) < NUM_LAYER2_GUARDS:
      self.add_new_layer2(generator)

    while len(self.layer3) < NUM_LAYER3_GUARDS:
      self.add_new_layer3(generator)

    plog("INFO", "New layer2 guards: "+self.layer2_guardset()+
                 " New layer3 guards: "+self.layer3_guardset())

  def _remove_down(self, remove_from, dict_r):
    removed = []
    for g in list(remove_from):
      if not g.idhex in dict_r:
        remove_from.remove(g)
        removed.append(g)
    return removed

  def consensus_update(self, dict_r, generator):
    # If any guards are down, remove them from current
    self._remove_down(self.layer2, dict_r)
    self._remove_down(self.layer3, dict_r)

    while len(self.layer2) < NUM_LAYER2_GUARDS:
      self.add_new_layer2(generator)

    while len(self.layer3) < NUM_LAYER3_GUARDS:
      self.add_new_layer3(generator)

def configure_tor(controller, vanguard_state):
  # XXX: Use NumPrimaryGuards.. or try to.
  if NUM_LAYER1_GUARDS:
    controller.set_conf("NumEntryGuards", str(NUM_LAYER1_GUARDS))

  if LAYER1_LIFETIME:
    controller.set_conf("GuardLifetime", str(LAYER1_LIFETIME)+" days")

  controller.set_conf("HSLayer2Nodes", vanguard_state.layer2_guardset())

  if NUM_LAYER3_GUARDS:
    controller.set_conf("HSLayer3Nodes", vanguard_state.layer3_guardset())

  controller.save_conf()

# TODO: This might be inefficient, because we just
# parsed the consensus for the event, and now we're parsing it
# again, twice.. Oh well. Prototype, and not critical path either.
def new_consensus_event(controller, state, options, event):
  (sorted_r, dict_r) = get_rlist_and_rdict(controller)
  weights = get_consensus_weights(controller)

  ng = BwWeightedGenerator(sorted_r,
                     NodeRestrictionList([FlagsRestriction(["Fast", "Stable"],
                                                           [])]),
                           weights, BwWeightedGenerator.POSITION_MIDDLE)
  gen = ng.generate()
  state.consensus_update(dict_r, gen)

  # XXX: Need to check this more often
  state.replace_expired(gen)
  state.rendguard.xfer_use_counts(ng)

  configure_tor(controller, state)
  state.write_to_file(open(options.state_file, "wb"))

class CircuitStat:
  def __init__(self, circ_id, is_hs):
    self.circ_id = circ_id
    self.is_hs = is_hs

class TimeoutStats:
  def __init__(self):
    self.circuits = {}
    self.all_launched = 0
    self.all_built = 0
    self.all_timeout = 0
    self.hs_launched = 0
    self.hs_built = 0
    self.hs_timeout = 0
    self.hs_changed = 0

  def add_circuit(self, circ_id, is_hs):
    if circ_id in self.circuits:
      plog("WARN", "Circuit "+circ_id+" already exists in map!")
    self.circuits[circ_id] = CircuitStat(circ_id, is_hs)
    self.all_launched += 1
    if is_hs: self.hs_launched += 1

  def update_circuit(self, circ_id, is_hs):
    if circ_id not in self.circuits: return
    if self.circuits[circ_id].is_hs != is_hs:
      self.hs_changed += 1
      self.hs_launched += 1
      self.circuits[circ_id].is_hs = is_hs

  def built_circuit(self, circ_id):
    if circ_id in self.circuits:
      self.all_built += 1
      if self.circuits[circ_id].is_hs:
        self.hs_built += 1
      del self.circuits[circ_id]

  def timeout_circuit(self, circ_id):
    if circ_id in self.circuits:
      self.all_timeout += 1
      if self.circuits[circ_id].is_hs:
        self.hs_timeout += 1
      del self.circuits[circ_id]

  # TODO: Sum launched == built+timeout+circuits

  def timeout_rate_all(self):
    if self.all_launched:
      return float(self.all_timeout)/(self.all_launched)
    else: return 0.0

  def timeout_rate_hs(self):
    if self.hs_launched:
      return float(self.hs_timeout)/(self.hs_launched)
    else: return 0.0

def circuit_event(state, timeouts, event):
  if event.hs_state or event.purpose[0:2] == "HS":
    if "status" in event.__dict__:
      if event.status == "LAUNCHED":
        timeouts.add_circuit(event.id, 1)
      elif event.status == "BUILT":
        timeouts.built_circuit(event.id)
      elif event.reason == "TIMEOUT":
        timeouts.timeout_circuit(event.id)
      timeouts.update_circuit(event.id, 1)
    if "status" in event.__dict__ and event.status == "BUILT" and \
       event.purpose == "HS_SERVICE_REND":
      state.rendguards.check_rend_use(event.purpose, event.path)
  elif "status" in event.__dict__:
    if event.status == "LAUNCHED":
      timeouts.add_circuit(event.id, 0)
    elif event.status == "BUILT":
      timeouts.built_circuit(event.id)
    elif event.reason == "TIMEOUT":
      timeouts.timeout_circuit(event.id)

  if "status" in event.__dict__ and event.status == "CLOSED":
    state.forget_circuit(event.id)

  plog("DEBUG", event.raw_content())

def cbt_event(timeouts, event):
  # XXX: Check if this is too high...
  plog("INFO", "CBT Timeout rate: "+str(event.timeout_rate)+"; Our measured timeout rate: "+str(timeouts.timeout_rate_all())+"; Hidden service timeout rate: "+str(timeouts.timeout_rate_hs()))
  plog("DEBUG", event.raw_content())

def main():
  options = setup_options()
  try:
    # XXX: Get tor's data directory
    f = open(options.state_file, "rb")
    state = VanguardState.read_from_file(f)
  except:
    state = VanguardState()

  controller = connect()
  new_consensus_event(controller, state, options, None)
  timeouts = TimeoutStats()
  bandwidths = BandwidthStats(controller)

  # Thread-safety: state, timeouts, and bandwidths are effectively
  # transferred to the event thread here. They must not be used in
  # our thread anymore.
  circuit_handler = functools.partial(circuit_event, state, timeouts)
  controller.add_event_listener(circuit_handler,
                                stem.control.EventType.CIRC)
  controller.add_event_listener(circuit_handler,
                                stem.control.EventType.CIRC_MINOR)


  controller.add_event_listener(
               functools.partial(BandwidthStats.circ_event, bandwidths),
                                stem.control.EventType.CIRC)
  controller.add_event_listener(
               functools.partial(BandwidthStats.bw_event, bandwidths),
                                stem.control.EventType.BW)
  controller.add_event_listener(
               functools.partial(BandwidthStats.circbw_event, bandwidths),
                                stem.control.EventType.CIRC_BW)

  cbt_handler = functools.partial(cbt_event, timeouts)
  controller.add_event_listener(cbt_handler,
                                stem.control.EventType.BUILDTIMEOUT_SET)

  # Thread-safety: We're effectively transferring controller to the event
  # thread here.
  new_consensus_handler = functools.partial(new_consensus_event,
                                            controller, state, options)
  controller.add_event_listener(new_consensus_handler,
                                stem.control.EventType.NEWCONSENSUS)


  # Blah...
  while controller.is_alive():
    time.sleep(1)

if __name__ == '__main__':
  main()