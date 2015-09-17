# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""The main OpenHTF entry point."""

import inspect
import itertools
import logging
import os
import signal
import socket
import sys
from json import JSONEncoder

import gflags

from openhtf import conf
from openhtf import exe
from openhtf import plugs
from openhtf.exe import test_state
from openhtf.io import http_handler
from openhtf.io import rundata
from openhtf.io import user_input
from openhtf.util import measurements
from openhtf.util import parameters


FLAGS = gflags.FLAGS
FLAGS(sys.argv)


class InvalidTestPhaseError(Exception):
  """Raised when an invalid method is decorated."""


# Pseudomodule for shared user input prompt state.
prompter = user_input.get_prompter()  # pylint: disable=invalid-name


class OutputToJson(JSONEncoder):
  """Return an output callback that writes JSON Test Records.

  An example filename_pattern might be:
    '/data/test_records/%(dut_id)s.%(start_time_millis)s'

  To use this output mechanism:
    test = openhtf.Test(PhaseOne, PhaseTwo)
    test.AddOutputCallback(openhtf.OutputToJson(
        '/data/test_records/%(dut_id)s.%(start_time_millis)s'))

  Args:
    filename_pattern: A format string specifying the filename to write to,
      will be formatted with the Test Record as a dictionary.
  """

  def __init__(self, filename_pattern, **kwargs):
    super(OutputToJson, self).__init__(**kwargs)
    self.filename_pattern = filename_pattern

  def default(self, obj):
    # Handle a few custom objects that end up in our output.
    if isinstance(obj, BaseException):
      # Just repr exceptions.
      return repr(obj)
    if isinstance(obj, conf.Config):
      return obj.dictionary
    if obj in test_state.TestState.State:
      return str(obj)
    return super(OutputToJson, self).default(obj)

  @classmethod
  def _ConvertToDict(cls, obj):
    """Recursively convert namedtuples to dicts."""
    if hasattr(obj, '_asdict'):
      obj = obj._asdict()

    # Recursively convert values in dicts, lists, and tuples.
    if isinstance(obj, dict):
      for key, value in obj.iteritems():
        obj[key] = cls._ConvertToDict(value)
    elif isinstance(obj, list):
      obj = [cls._ConvertToDict(value) for value in obj]
    elif isinstance(obj, tuple):
      obj = tuple(cls._ConvertToDict(value) for value in obj)

    return obj

  def __call__(self, test_record):  # pylint: disable=invalid-name
    with open(self.filename_pattern % test_record._asdict(), 'w') as f:
      f.write(self.encode(self._ConvertToDict(test_record)))


def TestPhase(timeout_s=None, run_if=None):  # pylint: disable=invalid-name
  """Decorator to wrap a test phase function with the given options.

  Args:
    timeout_s: Timeout to use for the phase, in seconds.
    run_if: Callback that decides whether to run the phase or not.  The
      callback will be passed the phase_data the phase would be run with.

  Returns:
    A wrapper function that takes a phase_func and returns a
        TestPhaseInfo for it with the given options set.
  """

  def Wrap(phase_func):  # pylint: disable=invalid-name
    """Attach the given options to the phase_func."""

    # Test Phases must take at least one argument (the phase data tuple).
    if len(inspect.getargspec(phase_func).args) < 1:
      raise InvalidTestPhaseError(phase_func, 'Not enough args')

    if timeout_s is not None:
      phase_func.timeout_s = timeout_s
    if run_if is not None:
      phase_func.run_if = run_if
    return phase_func
  return Wrap


class Test(object):
  """An object that represents an OpenHTF test.

  This object encapsulates the static test state including an ordered tuple of
  phases to execute.

  Args:
    *phases: The ordered list of phases to execute for this test.
  """

  def __init__(self, *phases):
    """Creates a new Test to be executed.
    Args:
      *phases: The ordered list of phases to execute for this test.
    """
    self.phases = phases
    self.output_callbacks = []

    # Pull some metadata from the frame in which this Test was created.
    frame_record = inspect.stack()[1]
    self.filename = os.path.basename(frame_record[1])
    self.docstring = inspect.getdoc(inspect.getmodule(frame_record[0]))
    self.code = inspect.getsource(frame_record[0])
    
    # TODO(jethier): Do something similar to this with measurements and
    # attachments.
    # Parameters can be directly attached to phases so we union the lists.
    self.parameters = parameters.TestParameterList.Union(
        *(phase.parameters for phase in self.phases
          if hasattr(phase, 'parameters')))
    
  @property
  def plug_type_map(self):
    """Returns dict mapping name to plug type for all phases."""
    plug_type_map = {}
    for plug, plug_type in itertools.chain.from_iterable(
        phase.plugs.iteritems() for phase in self.phases
        if hasattr(phase, 'plugs')):
      if (plug in plug_type_map and
          plug_type is not plug_type_map[plug]):
        raise plugs.DuplicatePlugError(
            'Duplicate plug with different type: %s' % plug)
      plug_type_map[plug] = plug_type
    return plug_type_map

  def AddOutputCallback(self, callback):
    self.output_callbacks.append(callback)

  def OutputTestRecord(self, test_record):
    for output_cb in self.output_callbacks:
      output_cb(test_record)

  # TODO(madsci): Execute loops indefinitely right now, we should probably
  # provide an 'ExecuteOnce' method you can call instead if you don't want
  # to loop.
  def Execute(self):
    """Start the OpenHTF framework running with the given test.
  
    Executes this test, iterating over self.phases and executing them.

    Example:
 
      def PhaseOne(test):
        # Integrate more whidgets
  
      def PhaseTwo(test):
        # Analyze whidget integration status
  
      Test(PhaseOne, PhaseTwo).Execute()
  
    Returns:
      None when the test framework has exited.
    """
    conf.Load()
  
    config = conf.Config()
    rundata.RunData(self.filename,
                    len(config.cell_info),
# TODO(madsci/jethier): Update rundata interface, these are dummy values.
                    config.station_id,
                    '0.1',
                    socket.gethostname(),
                    FLAGS.http_port,
                    os.getpid()).SaveToFile(FLAGS.rundir)
  
    logging.info('Executing test: %s', self.filename)
    starter = exe.TestExecutorStarter(self)
    handler = http_handler.HttpHandler(self, starter.cells)
  
    def sigint_handler(*dummy):
      """Handle SIGINT by stopping running cells."""
      print "Received SIGINT. Stopping everything."
      starter.Stop()
      handler.Stop()
    signal.signal(signal.SIGINT, sigint_handler)
  
    handler.Start()
    starter.Start()
  
    starter.Wait()
    handler.Stop()
    return


# Pseudomodule for shared user input prompt state.
prompter = user_input.get_prompter()  # pylint: disable=invalid-name
