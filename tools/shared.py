from __future__ import print_function

from distutils.spawn import find_executable
from subprocess import PIPE, STDOUT
import atexit
import base64
import json
import logging
import math
import multiprocessing
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from .toolchain_profiler import ToolchainProfiler
from .tempfiles import try_delete
from . import jsrun, cache, tempfiles, colored_logger
from . import response_file

colored_logger.enable()

__rootpath__ = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
WINDOWS = sys.platform.startswith('win')
MACOS = sys.platform == 'darwin'
LINUX = sys.platform.startswith('linux')


class FatalError(Exception):
  """Error representing an unrecoverable error such as the failure of
  a subprocess.

  These are usually handled at the entry point of each script."""
  pass


def exit_with_error(*args):
  logging.error(*args)
  sys.exit(1)


# On Windows python suffers from a particularly nasty bug if python is spawning
# new processes while python itself is spawned from some other non-console
# process.
# Use a custom replacement for Popen on Windows to avoid the "WindowsError:
# [Error 6] The handle is invalid" errors when emcc is driven through cmake or
# mingw32-make.
# See http://bugs.python.org/issue3905
class WindowsPopen(object):
  def __init__(self, args, bufsize=0, executable=None, stdin=None, stdout=None, stderr=None, preexec_fn=None, close_fds=False,
               shell=False, cwd=None, env=None, universal_newlines=False, startupinfo=None, creationflags=0):
    self.stdin = stdin
    self.stdout = stdout
    self.stderr = stderr

    # (stdin, stdout, stderr) store what the caller originally wanted to be done with the streams.
    # (stdin_, stdout_, stderr_) will store the fixed set of streams that workaround the bug.
    self.stdin_ = stdin
    self.stdout_ = stdout
    self.stderr_ = stderr

    # If the caller wants one of these PIPEd, we must PIPE them all to avoid the 'handle is invalid' bug.
    if self.stdin_ == PIPE or self.stdout_ == PIPE or self.stderr_ == PIPE:
      if self.stdin_ is None:
        self.stdin_ = PIPE
      if self.stdout_ is None:
        self.stdout_ = PIPE
      if self.stderr_ is None:
        self.stderr_ = PIPE

    # emscripten.py supports reading args from a response file instead of cmdline.
    # Use .rsp to avoid cmdline length limitations on Windows.
    if len(args) >= 2 and args[1].endswith("emscripten.py"):
      response_filename = response_file.create_response_file(args[2:], TEMP_DIR)
      args = args[0:2] + ['@' + response_filename]

    try:
      # Call the process with fixed streams.
      self.process = subprocess.Popen(args, bufsize, executable, self.stdin_, self.stdout_, self.stderr_, preexec_fn, close_fds, shell, cwd, env, universal_newlines, startupinfo, creationflags)
      self.pid = self.process.pid
    except Exception as e:
      logging.error('\nsubprocess.Popen(args=%s) failed! Exception %s\n' % (' '.join(args), str(e)))
      raise e

  def communicate(self, input=None):
    output = self.process.communicate(input)
    self.returncode = self.process.returncode

    # If caller never wanted to PIPE stdout or stderr, route the output back to screen to avoid swallowing output.
    if self.stdout is None and self.stdout_ == PIPE and len(output[0].strip()):
      print(output[0], file=sys.stdout)
    if self.stderr is None and self.stderr_ == PIPE and len(output[1].strip()):
      print(output[1], file=sys.stderr)

    # Return a mock object to the caller. This works as long as all emscripten code immediately .communicate()s the result, and doesn't
    # leave the process object around for longer/more exotic uses.
    if self.stdout is None and self.stderr is None:
      return (None, None)
    if self.stdout is None:
      return (None, output[1])
    if self.stderr is None:
      return (output[0], None)
    return (output[0], output[1])

  def poll(self):
    return self.process.poll()

  def kill(self):
    return self.process.kill()


def path_from_root(*pathelems):
  return os.path.join(__rootpath__, *pathelems)


# This is a workaround for https://bugs.python.org/issue9400
class Py2CalledProcessError(subprocess.CalledProcessError):
  def __init__(self, returncode, cmd, output=None, stderr=None):
    super(Exception, self).__init__(returncode, cmd, output, stderr)
    self.returncode = returncode
    self.cmd = cmd
    self.output = output
    self.stderr = stderr


# https://docs.python.org/3/library/subprocess.html#subprocess.CompletedProcess
class Py2CompletedProcess:
  def __init__(self, args, returncode, stdout, stderr):
    self.args = args
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr

  def __repr__(self):
    _repr = ['args=%s, returncode=%s' % (self.args, self.returncode)]
    if self.stdout is not None:
      _repr += 'stdout=' + repr(self.stdout)
    if self.stderr is not None:
      _repr += 'stderr=' + repr(self.stderr)
    return 'CompletedProcess(%s)' % ', '.join(_repr)

  def check_returncode(self):
    if self.returncode is not 0:
      raise Py2CalledProcessError(returncode=self.returncode, cmd=self.args, output=self.stdout, stderr=self.stderr)


def run_base(cmd, check=False, input=None, *args, **kw):
  if hasattr(subprocess, "run"):
    return subprocess.run(cmd, check=check, input=input, *args, **kw)

  # Python 2 compatibility: Introduce Python 3 subprocess.run-like behavior
  if input is not None:
    kw['stdin'] = subprocess.PIPE
  proc = Popen(cmd, *args, **kw)
  stdout, stderr = proc.communicate(input)
  result = Py2CompletedProcess(cmd, proc.returncode, stdout, stderr)
  if check:
    result.check_returncode()
  return result


def run_process(cmd, universal_newlines=True, check=True, *args, **kw):
  return run_base(cmd, universal_newlines=universal_newlines, check=check, *args, **kw)


def check_execute(cmd, *args, **kw):
  try:
    run_process(cmd, stdout=PIPE, *args, **kw)
    logging.debug("Successfuly executed %s" % " ".join(cmd))
  except subprocess.CalledProcessError as e:
    raise FatalError("'%s' failed with output:\n%s" % (" ".join(e.cmd), e.output))


def check_call(cmd, *args, **kw):
  try:
    run_process(cmd, *args, **kw)
    logging.debug("Successfully executed %s" % " ".join(cmd))
  except subprocess.CalledProcessError:
    raise FatalError("'%s' failed" % " ".join(cmd))


def generate_config(path, first_time=False):
  # Note: repr is used to ensure the paths are escaped correctly on Windows.
  # The full string is replaced so that the template stays valid Python.
  config_file = open(path_from_root('tools', 'settings_template_readonly.py')).read().split('\n')
  config_file = config_file[1:] # remove "this file will be copied..."
  config_file = '\n'.join(config_file)
  # autodetect some default paths
  config_file = config_file.replace('\'{{{ EMSCRIPTEN_ROOT }}}\'', repr(__rootpath__))
  llvm_root = os.path.dirname(find_executable('llvm-dis') or '/usr/bin/llvm-dis')
  config_file = config_file.replace('\'{{{ LLVM_ROOT }}}\'', repr(llvm_root))

  node = find_executable('nodejs') or find_executable('node') or 'node'
  config_file = config_file.replace('\'{{{ NODE }}}\'', repr(node))
  if WINDOWS:
    tempdir = os.environ.get('TEMP') or os.environ.get('TMP') or 'c:\\temp'
  else:
    tempdir = '/tmp'
  config_file = config_file.replace('\'{{{ TEMP }}}\'', repr(tempdir))

  abspath = os.path.abspath(os.path.expanduser(path))
  # write
  open(abspath, 'w').write(config_file)
  if first_time:
    print('''
==============================================================================
Welcome to Emscripten!

This is the first time any of the Emscripten tools has been run.

A settings file has been copied to %s, at absolute path: %s

It contains our best guesses for the important paths, which are:

  LLVM_ROOT       = %s
  NODE_JS         = %s
  EMSCRIPTEN_ROOT = %s

Please edit the file if any of those are incorrect.

This command will now exit. When you are done editing those paths, re-run it.
==============================================================================
''' % (path, abspath, llvm_root, node, __rootpath__), file=sys.stderr)


# Emscripten configuration is done through the --em-config command line option or
# the EM_CONFIG environment variable. If the specified string value contains newline
# or semicolon-separated definitions, then these definitions will be used to configure
# Emscripten.  Otherwise, the string is understood to be a path to a settings
# file that contains the required definitions.

try:
  EM_CONFIG = sys.argv[sys.argv.index('--em-config') + 1]
  # And now remove it from sys.argv
  skip = False
  newargs = []
  for arg in sys.argv:
    if not skip and arg != '--em-config':
      newargs += [arg]
    elif arg == '--em-config':
      skip = True
    elif skip:
      skip = False
  sys.argv = newargs
  # Emscripten compiler spawns other processes, which can reimport shared.py, so make sure that
  # those child processes get the same configuration file by setting it to the currently active environment.
  os.environ['EM_CONFIG'] = EM_CONFIG
except:
  EM_CONFIG = os.environ.get('EM_CONFIG')

if EM_CONFIG and not os.path.isfile(EM_CONFIG):
  if EM_CONFIG.startswith('-'):
    raise Exception('Passed --em-config without an argument. Usage: --em-config /path/to/.emscripten or --em-config EMSCRIPTEN_ROOT=/path/;LLVM_ROOT=/path;...')
  if '=' not in EM_CONFIG:
    raise Exception('File ' + EM_CONFIG + ' passed to --em-config does not exist!')
  else:
    EM_CONFIG = EM_CONFIG.replace(';', '\n') + '\n'

if not EM_CONFIG:
  EM_CONFIG = '~/.emscripten'
if '\n' in EM_CONFIG:
  CONFIG_FILE = None
  logging.debug('EM_CONFIG is specified inline without a file')
else:
  CONFIG_FILE = os.path.expanduser(EM_CONFIG)
  logging.debug('EM_CONFIG is located in ' + CONFIG_FILE)
  if not os.path.exists(CONFIG_FILE):
    generate_config(EM_CONFIG, first_time=True)
    sys.exit(0)

# The following globals can be overridden by the config file.
EMSCRIPTEN_ROOT = __rootpath__
NODE_JS = None
BINARYEN_ROOT = None
EM_POPEN_WORKAROUND = None
SPIDERMONKEY_ENGINE = None
V8_ENGINE = None
LLVM_ROOT = None
COMPILER_ENGINE = None
LLVM_ADD_VERSION = None
CLANG_ADD_VERSION = None
CLOSURE_COMPILER = None
JAVA = None
PYTHON = None
JS_ENGINE = None
COMPILER_OPTS = []

try:
  config_text = open(CONFIG_FILE, 'r').read() if CONFIG_FILE else EM_CONFIG
  exec(config_text)
except Exception as e:
  logging.error('Error in evaluating %s (at %s): %s, text: %s' % (EM_CONFIG, CONFIG_FILE, str(e), config_text))
  sys.exit(1)

# EMSCRIPTEN_ROOT is set in the config file so that external tools such as
# scons can find emscripten by looking at the config.  Although its not used
# within emscripten itself this is good to time sanity check the value.
EMSCRIPTEN_ROOT = os.path.expanduser(os.path.normpath(EMSCRIPTEN_ROOT))
if EMSCRIPTEN_ROOT != __rootpath__:
  exit_with_error('Incorrect EMSCRIPTEN_ROOT in config file: %s (Expected %s)', EMSCRIPTEN_ROOT, __rootpath__)


# Returns a suggestion where current .emscripten config file might be located
# (if EM_CONFIG env. var is used without a file, this hints to "default"
# location at ~/.emscripten)
def hint_config_file_location():
  if CONFIG_FILE:
    return CONFIG_FILE
  else:
    return '~/.emscripten'


def listify(x):
  if type(x) is not list:
    return [x]
  return x


def fix_js_engine(old, new):
  if old is None:
    return
  global JS_ENGINES
  JS_ENGINES = [new if x == old else x for x in JS_ENGINES]
  return new


SPIDERMONKEY_ENGINE = fix_js_engine(SPIDERMONKEY_ENGINE, listify(SPIDERMONKEY_ENGINE))
NODE_JS = fix_js_engine(NODE_JS, listify(NODE_JS))
V8_ENGINE = fix_js_engine(V8_ENGINE, listify(V8_ENGINE))
COMPILER_ENGINE = listify(COMPILER_ENGINE)
JS_ENGINES = [listify(engine) for engine in JS_ENGINES]

if EM_POPEN_WORKAROUND is None:
  EM_POPEN_WORKAROUND = os.environ.get('EM_POPEN_WORKAROUND')

# Install our replacement Popen handler if we are running on Windows to avoid
# python spawn process function.
# nb. This is by default disabled since it has the adverse effect of buffering
# up all logging messages, which makes builds look unresponsive (messages are
# printed only after the whole build finishes). Whether this workaround is
# needed seems to depend on how the host application that invokes emcc has set
# up its stdout and stderr.
if EM_POPEN_WORKAROUND and os.name == 'nt':
  logging.debug('Installing Popen workaround handler to avoid bug http://bugs.python.org/issue3905')
  Popen = WindowsPopen
else:
  Popen = subprocess.Popen

# Verbosity level control for any intermediate subprocess spawns from the compiler. Useful for internal debugging.
# 0: disabled.
# 1: Log stderr of subprocess spawns.
# 2: Log stdout and stderr of subprocess spawns. Print out subprocess commands that were executed.
# 3: Log stdout and stderr, and pass VERBOSE=1 to CMake configure steps.
EM_BUILD_VERBOSE_LEVEL = int(os.getenv('EM_BUILD_VERBOSE', '0'))

# Expectations

actual_clang_version = None


def expected_llvm_version():
  if get_llvm_target() == WASM_TARGET:
    return "7.0"
  else:
    return "6.0"


def get_clang_version():
  global actual_clang_version
  if actual_clang_version is None:
    response = run_process([CLANG, '-v'], stderr=PIPE).stderr
    m = re.search(r'[Vv]ersion\s+(\d+\.\d+)', response)
    actual_clang_version = m and m.group(1)
  return actual_clang_version


def check_clang_version():
  expected = expected_llvm_version()
  actual = get_clang_version()
  if expected in actual:
    return True
  logging.warning('LLVM version appears incorrect (seeing "%s", expected "%s")' % (actual, expected))
  return False


def check_llvm_version():
  try:
    check_clang_version()
  except Exception as e:
    logging.critical('Could not verify LLVM version: %s' % str(e))


# look for emscripten-version.txt files under or alongside the llvm source dir
def get_fastcomp_src_dir():
  d = LLVM_ROOT
  emroot = path_from_root() # already abspath
  # look for version file in llvm repo, making sure not to mistake the emscripten repo for it
  while d != os.path.dirname(d):
    d = os.path.abspath(d)
    # when the build directory lives below the source directory
    if os.path.exists(os.path.join(d, 'emscripten-version.txt')) and not d == emroot:
      return d
    # when the build directory lives alongside the source directory
    elif os.path.exists(os.path.join(d, 'src', 'emscripten-version.txt')) and not os.path.join(d, 'src') == emroot:
      return os.path.join(d, 'src')
    else:
      d = os.path.dirname(d)
  return None


def get_llc_targets():
  try:
    llc_version_info = run_process([LLVM_COMPILER, '--version'], stdout=PIPE).stdout
    pre, targets = llc_version_info.split('Registered Targets:')
    return targets
  except Exception as e:
    return '(no targets could be identified: ' + str(e) + ')'


def has_asm_js_target(targets):
  return 'js' in targets and 'JavaScript (asm.js, emscripten) backend' in targets


def has_wasm_target(targets):
  return 'wasm32' in targets and 'WebAssembly 32-bit' in targets


def check_fastcomp():
  try:
    targets = get_llc_targets()
    if not Settings.WASM_BACKEND:
      if not has_asm_js_target(targets):
        logging.critical('fastcomp in use, but LLVM has not been built with the JavaScript backend as a target, llc reports:')
        print('===========================================================================', file=sys.stderr)
        print(targets, file=sys.stderr)
        print('===========================================================================', file=sys.stderr)
        logging.critical('you can fall back to the older (pre-fastcomp) compiler core, although that is not recommended, see http://kripken.github.io/emscripten-site/docs/building_from_source/LLVM-Backend.html')
        return False
    else:
      if not has_wasm_target(targets):
        logging.critical('WebAssembly set as target, but LLVM has not been built with the WebAssembly backend, llc reports:')
        print('===========================================================================', file=sys.stderr)
        print(targets, file=sys.stderr)
        print('===========================================================================', file=sys.stderr)
        return False

    if not Settings.WASM_BACKEND:
      # check repo versions
      d = get_fastcomp_src_dir()
      shown_repo_version_error = False
      if d is not None:
        llvm_version = get_emscripten_version(os.path.join(d, 'emscripten-version.txt'))
        if os.path.exists(os.path.join(d, 'tools', 'clang', 'emscripten-version.txt')):
          clang_version = get_emscripten_version(os.path.join(d, 'tools', 'clang', 'emscripten-version.txt'))
        elif os.path.exists(os.path.join(d, 'tools', 'clang')):
          clang_version = '?' # Looks like the LLVM compiler tree has an old checkout from the time before it contained a version.txt: Should update!
        else:
          clang_version = llvm_version # This LLVM compiler tree does not have a tools/clang, so it's probably an out-of-source build directory. No need for separate versioning.
        if EMSCRIPTEN_VERSION != llvm_version or EMSCRIPTEN_VERSION != clang_version:
          logging.error('Emscripten, llvm and clang repo versions do not match, this is dangerous (%s, %s, %s)', EMSCRIPTEN_VERSION, llvm_version, clang_version)
          logging.error('Make sure to use the same branch in each repo, and to be up-to-date on each. See http://kripken.github.io/emscripten-site/docs/building_from_source/LLVM-Backend.html')
          shown_repo_version_error = True
      else:
        logging.warning('did not see a source tree above or next to the LLVM root directory (guessing based on directory of %s), could not verify version numbers match' % LLVM_COMPILER)

      # check build versions. don't show it if the repos are wrong, user should fix that first
      if not shown_repo_version_error:
        clang_v = run_process([CLANG, '--version'], stdout=PIPE).stdout
        llvm_build_version, clang_build_version = clang_v.split('(emscripten ')[1].split(')')[0].split(' : ')
        if EMSCRIPTEN_VERSION != llvm_build_version or EMSCRIPTEN_VERSION != clang_build_version:
          logging.error('Emscripten, llvm and clang build versions do not match, this is dangerous (%s, %s, %s)', EMSCRIPTEN_VERSION, llvm_build_version, clang_build_version)
          logging.error('Make sure to rebuild llvm and clang after updating repos')

    return True
  except Exception as e:
    logging.warning('could not check fastcomp: %s' % str(e))
    return True


EXPECTED_NODE_VERSION = (4, 1, 1)


def check_node_version():
  jsrun.check_engine(NODE_JS)
  try:
    actual = run_process(NODE_JS + ['--version'], stdout=PIPE).stdout.strip()
    version = tuple(map(int, actual.replace('v', '').replace('-pre', '').split('.')))
    if version >= EXPECTED_NODE_VERSION:
      return True
    logging.warning('node version appears too old (seeing "%s", expected "%s")' % (actual, 'v' + ('.'.join(map(str, EXPECTED_NODE_VERSION)))))
    return False
  except Exception as e:
    logging.warning('cannot check node version: %s', e)
    return False


def check_closure_compiler():
  try:
    run_process([JAVA, '-version'], stdout=PIPE, stderr=PIPE)
  except:
    logging.warning('java does not seem to exist, required for closure compiler, which is optional (define JAVA in ' + hint_config_file_location() + ' if you want it)')
    return False
  if not os.path.exists(CLOSURE_COMPILER):
    logging.warning('Closure compiler (%s) does not exist, check the paths in %s' % (CLOSURE_COMPILER, EM_CONFIG))
    return False
  return True


# Finds the system temp directory without resorting to using the one configured in .emscripten
def find_temp_directory():
  if WINDOWS:
    if os.getenv('TEMP') and os.path.isdir(os.getenv('TEMP')):
      return os.getenv('TEMP')
    elif os.getenv('TMP') and os.path.isdir(os.getenv('TMP')):
      return os.getenv('TMP')
    elif os.path.isdir('C:\\temp'):
      return os.getenv('C:\\temp')
    else:
      return None # No luck!
  else:
    return '/tmp'


def get_emscripten_version(path):
  return open(path).read().strip().replace('"', '')


EMSCRIPTEN_VERSION = get_emscripten_version(path_from_root('emscripten-version.txt'))
parts = [int(x) for x in EMSCRIPTEN_VERSION.split('.')]
EMSCRIPTEN_VERSION_MAJOR, EMSCRIPTEN_VERSION_MINOR, EMSCRIPTEN_VERSION_TINY = parts


def generate_sanity():
  return EMSCRIPTEN_VERSION + '|' + LLVM_ROOT + '|' + get_clang_version() + ('_wasm' if Settings.WASM_BACKEND else '')


def check_sanity(force=False):
  """Check that basic stuff we need (a JS engine to compile, Node.js, and Clang
  and LLVM) exists.

  The test runner always does this check (through |force|). emcc does this less
  frequently, only when ${EM_CONFIG}_sanity does not exist or is older than
  EM_CONFIG (so, we re-check sanity when the settings are changed).  We also
  re-check sanity and clear the cache when the version changes"""
  ToolchainProfiler.enter_block('sanity')
  try:
    if os.environ.get('EMCC_SKIP_SANITY_CHECK') == '1':
      return
    reason = None
    if not CONFIG_FILE:
      return # config stored directly in EM_CONFIG => skip sanity checks
    else:
      settings_mtime = os.stat(CONFIG_FILE).st_mtime
      sanity_file = CONFIG_FILE + '_sanity'
      if Settings.WASM_BACKEND:
        sanity_file += '_wasm'
      if os.path.exists(sanity_file):
        try:
          sanity_mtime = os.stat(sanity_file).st_mtime
          if sanity_mtime <= settings_mtime:
            reason = 'settings file has changed'
          else:
            sanity_data = open(sanity_file).read().rstrip('\n\r') # workaround weird bug with read() that appends new line char in some old python version
            if sanity_data != generate_sanity():
              reason = 'system change: %s vs %s' % (generate_sanity(), sanity_data)
            else:
              if not force:
                return # all is well
        except Exception as e:
          reason = 'unknown: ' + str(e)
    if reason:
      logging.warning('(Emscripten: %s, clearing cache)' % reason)
      Cache.erase()
      force = False # the check actually failed, so definitely write out the sanity file, to avoid others later seeing failures too

    # some warning, mostly not fatal checks - do them even if EM_IGNORE_SANITY is on
    check_llvm_version()
    check_node_version()

    if os.environ.get('EMCC_FAST_COMPILER') == '0':
      logging.critical('Non-fastcomp compiler is no longer available, please use fastcomp or an older version of emscripten')
      sys.exit(1)

    fastcomp_ok = check_fastcomp()

    if os.environ.get('EM_IGNORE_SANITY'):
      logging.info('EM_IGNORE_SANITY set, ignoring sanity checks')
      return

    logging.info('(Emscripten: Running sanity checks)')

    with ToolchainProfiler.profile_block('sanity compiler_engine'):
      if not jsrun.check_engine(COMPILER_ENGINE):
        exit_with_error('The JavaScript shell used for compiling (%s) does not seem to work, check the paths in %s', COMPILER_ENGINE, EM_CONFIG)

    with ToolchainProfiler.profile_block('sanity LLVM'):
      for cmd in [CLANG, LLVM_LINK, LLVM_AR, LLVM_OPT, LLVM_AS, LLVM_DIS, LLVM_NM, LLVM_INTERPRETER]:
        if not os.path.exists(cmd) and not os.path.exists(cmd + '.exe'):  # .exe extension required for Windows
          exit_with_error('Cannot find %s, check the paths in %s', cmd, EM_CONFIG)

    if not os.path.exists(PYTHON) and not os.path.exists(cmd + '.exe'):
      try:
        run_process([PYTHON, '--xversion'], stdout=PIPE, stderr=PIPE)
      except:
        exit_with_error('Cannot find %s, check the paths in %s', PYTHON, EM_CONFIG)

    if not fastcomp_ok:
      exit_with_error('failing sanity checks due to previous fastcomp failure')

    # Sanity check passed!
    with ToolchainProfiler.profile_block('sanity closure compiler'):
      if not check_closure_compiler():
        logging.warning('closure compiler will not be available')

    if not force:
      # Only create/update this file if the sanity check succeeded, i.e., we got here
      f = open(sanity_file, 'w')
      f.write(generate_sanity())
      f.close()

  except Exception as e:
    # Any error here is not worth failing on
    print('WARNING: sanity check failed to run', e)
  finally:
    ToolchainProfiler.exit_block('sanity')


# Tools/paths
if LLVM_ADD_VERSION is None:
  LLVM_ADD_VERSION = os.getenv('LLVM_ADD_VERSION')

if CLANG_ADD_VERSION is None:
  CLANG_ADD_VERSION = os.getenv('CLANG_ADD_VERSION')


# Some distributions ship with multiple llvm versions so they add
# the version to the binaries, cope with that
def build_llvm_tool_path(tool):
  if LLVM_ADD_VERSION:
    return os.path.join(LLVM_ROOT, tool + "-" + LLVM_ADD_VERSION)
  else:
    return os.path.join(LLVM_ROOT, tool)


# Some distributions ship with multiple clang versions so they add
# the version to the binaries, cope with that
def build_clang_tool_path(tool):
  if CLANG_ADD_VERSION:
    return os.path.join(LLVM_ROOT, tool + "-" + CLANG_ADD_VERSION)
  else:
    return os.path.join(LLVM_ROOT, tool)


# Whenever building a native executable for macOS, we must provide the macOS SDK
# version we want to target.
def macos_find_native_sdk_path():
  try:
    sdk_root = '/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs'
    sdks = os.walk(sdk_root).next()[1]
    sdk_path = os.path.join(sdk_root, sdks[0]) # Just pick first one found, we don't care which one we found.
    logging.debug('Targeting macOS SDK found at ' + sdk_path)
    return sdk_path
  except:
    logging.warning('Could not find native macOS SDK path to target!')
    return None


# These extra args need to be passed to Clang when targeting a native host system executable
CACHED_CLANG_NATIVE_ARGS = None


def get_clang_native_args():
  global CACHED_CLANG_NATIVE_ARGS
  if CACHED_CLANG_NATIVE_ARGS is not None:
    return CACHED_CLANG_NATIVE_ARGS
  CACHED_CLANG_NATIVE_ARGS = []
  if MACOS:
    sdk_path = macos_find_native_sdk_path()
    if sdk_path:
      CACHED_CLANG_NATIVE_ARGS = ['-isysroot', macos_find_native_sdk_path()]
  elif os.name == 'nt':
    CACHED_CLANG_NATIVE_ARGS = ['-DWIN32']
    # TODO: If Windows.h et al. are needed, will need to add something like '-isystemC:/Program Files (x86)/Microsoft SDKs/Windows/v7.1A/Include'.
  return CACHED_CLANG_NATIVE_ARGS


# This environment needs to be present when targeting a native host system executable
CACHED_CLANG_NATIVE_ENV = None


def get_clang_native_env():
  global CACHED_CLANG_NATIVE_ENV
  if CACHED_CLANG_NATIVE_ENV is not None:
    return CACHED_CLANG_NATIVE_ENV
  env = os.environ.copy()

  if WINDOWS:
    # If already running in Visual Studio Command Prompt manually, no need to
    # add anything here, so just return.
    if 'VSINSTALLDIR' in env and 'INCLUDE' in env and 'LIB' in env:
      CACHED_CLANG_NATIVE_ENV = env
      return env

    # Guess where VS2015 is installed (VSINSTALLDIR env. var in VS2015 X64 Command Prompt)
    if 'VSINSTALLDIR' in env:
      visual_studio_path = env['VSINSTALLDIR']
    elif 'VS140COMNTOOLS' in env:
      visual_studio_path = os.path.normpath(os.path.join(env['VS140COMNTOOLS'], '../..'))
    elif 'ProgramFiles(x86)' in env:
      visual_studio_path = os.path.normpath(os.path.join(env['ProgramFiles(x86)'], 'Microsoft Visual Studio 14.0'))
    elif 'ProgramFiles' in env:
      visual_studio_path = os.path.normpath(os.path.join(env['ProgramFiles'], 'Microsoft Visual Studio 14.0'))
    else:
      visual_studio_path = 'C:\\Program Files (x86)\\Microsoft Visual Studio 14.0'
    if not os.path.isdir(visual_studio_path):
      raise Exception('Visual Studio 2015 was not found in "' + visual_studio_path + '"! Run in Visual Studio X64 command prompt to avoid the need to autoguess this location (or set VSINSTALLDIR env var).')

    # Guess where Program Files (x86) is located
    if 'ProgramFiles(x86)' in env:
      prog_files_x86 = env['ProgramFiles(x86)']
    elif 'ProgramFiles' in env:
      prog_files_x86 = env['ProgramFiles']
    elif os.path.isdir('C:\\Program Files (x86)'):
      prog_files_x86 = 'C:\\Program Files (x86)'
    elif os.path.isdir('C:\\Program Files'):
      prog_files_x86 = 'C:\\Program Files'
    else:
      raise Exception('Unable to detect Program files directory for native Visual Studio build!')

    # Guess where Windows 8.1 SDK is located
    if 'WindowsSdkDir' in env:
      windows8_sdk_dir = env['WindowsSdkDir']
    elif os.path.isdir(os.path.join(prog_files_x86, 'Windows Kits', '8.1')):
      windows8_sdk_dir = os.path.join(prog_files_x86, 'Windows Kits', '8.1')
    if not os.path.isdir(windows8_sdk_dir):
      raise Exception('Windows 8.1 SDK was not found in "' + windows8_sdk_dir + '"! Run in Visual Studio command prompt to avoid the need to autoguess this location (or set WindowsSdkDir env var).')

    # Guess where Windows 10 SDK is located
    if os.path.isdir(os.path.join(prog_files_x86, 'Windows Kits', '10')):
      windows10_sdk_dir = os.path.join(prog_files_x86, 'Windows Kits', '10')
    if not os.path.isdir(windows10_sdk_dir):
      raise Exception('Windows 10 SDK was not found in "' + windows10_sdk_dir + '"! Run in Visual Studio command prompt to avoid the need to autoguess this location.')

    env.setdefault('VSINSTALLDIR', visual_studio_path)
    env.setdefault('VCINSTALLDIR', os.path.join(visual_studio_path, 'VC'))

    windows10sdk_kits_include_dir = os.path.join(windows10_sdk_dir, 'Include')
    windows10sdk_kit_version_name = [x for x in os.listdir(windows10sdk_kits_include_dir) if os.path.isdir(os.path.join(windows10sdk_kits_include_dir, x))][0] # e.g. "10.0.10150.0" or "10.0.10240.0"

    def append_item(key, item):
      if key not in env or len(env[key].strip()) == 0:
        env[key] = item
      else:
        env[key] = env[key] + ';' + item

    append_item('INCLUDE', os.path.join(env['VCINSTALLDIR'], 'INCLUDE'))
    append_item('INCLUDE', os.path.join(env['VCINSTALLDIR'], 'ATLMFC', 'INCLUDE'))
    append_item('INCLUDE', os.path.join(windows10_sdk_dir, 'include', windows10sdk_kit_version_name, 'ucrt'))
    #   append_item('INCLUDE', 'C:\\Program Files (x86)\\Windows Kits\\NETFXSDK\\4.6.1\\include\\um') # VS2015 X64 command prompt has this, but not needed for Emscripten
    append_item('INCLUDE', os.path.join(env['VCINSTALLDIR'], 'ATLMFC', 'INCLUDE'))
    append_item('INCLUDE', os.path.join(windows8_sdk_dir, 'include', 'shared'))
    append_item('INCLUDE', os.path.join(windows8_sdk_dir, 'include', 'um'))
    append_item('INCLUDE', os.path.join(windows8_sdk_dir, 'include', 'winrt'))
    logging.debug('VS2015 native build INCLUDE: ' + env['INCLUDE'])

    append_item('LIB', os.path.join(env['VCINSTALLDIR'], 'LIB', 'amd64'))
    append_item('LIB', os.path.join(env['VCINSTALLDIR'], 'ATLMFC', 'LIB', 'amd64'))
    append_item('LIB', os.path.join(windows10_sdk_dir, 'lib', windows10sdk_kit_version_name, 'ucrt', 'x64'))
    #   append_item('LIB', 'C:\\Program Files (x86)\\Windows Kits\\NETFXSDK\\4.6.1\\lib\\um\\x64') # VS2015 X64 command prompt has this, but not needed for Emscripten
    append_item('LIB', os.path.join(windows8_sdk_dir, 'lib', 'winv6.3', 'um', 'x64'))
    logging.debug('VS2015 native build LIB: ' + env['LIB'])

    env['PATH'] = env['PATH'] + ';' + os.path.join(env['VCINSTALLDIR'], 'BIN')
    logging.debug('VS2015 native build PATH: ' + env['PATH'])

  # Current configuration above is all Visual Studio -specific, so on non-Windowses, no action needed.

  CACHED_CLANG_NATIVE_ENV = env
  return env


def exe_suffix(cmd):
  return cmd + '.exe' if WINDOWS else cmd


CLANG_CC = os.path.expanduser(build_clang_tool_path(exe_suffix('clang')))
CLANG_CPP = os.path.expanduser(build_clang_tool_path(exe_suffix('clang++')))
CLANG = CLANG_CPP
LLVM_LINK = build_llvm_tool_path(exe_suffix('llvm-link'))
LLVM_AR = build_llvm_tool_path(exe_suffix('llvm-ar'))
LLVM_OPT = os.path.expanduser(build_llvm_tool_path(exe_suffix('opt')))
LLVM_AS = os.path.expanduser(build_llvm_tool_path(exe_suffix('llvm-as')))
LLVM_DIS = os.path.expanduser(build_llvm_tool_path(exe_suffix('llvm-dis')))
LLVM_NM = os.path.expanduser(build_llvm_tool_path(exe_suffix('llvm-nm')))
LLVM_INTERPRETER = os.path.expanduser(build_llvm_tool_path(exe_suffix('lli')))
LLVM_COMPILER = os.path.expanduser(build_llvm_tool_path(exe_suffix('llc')))
LLVM_DWARFDUMP = os.path.expanduser(build_llvm_tool_path(exe_suffix('llvm-dwarfdump')))
WASM_LD = os.path.expanduser(build_llvm_tool_path(exe_suffix('wasm-ld')))

EMSCRIPTEN = path_from_root('emscripten.py')
EMCC = path_from_root('emcc.py')
EMXX = path_from_root('em++.py')
EMAR = path_from_root('emar.py')
EMRANLIB = path_from_root('emranlib')
EMCONFIG = path_from_root('em-config')
EMLINK = path_from_root('emlink.py')
EMMAKEN = path_from_root('tools', 'emmaken.py')
AUTODEBUGGER = path_from_root('tools', 'autodebugger.py')
EXEC_LLVM = path_from_root('tools', 'exec_llvm.py')
FILE_PACKAGER = path_from_root('tools', 'file_packager.py')


# Temp dir. Create a random one, unless EMCC_DEBUG is set, in which case use TEMP_DIR/emscripten_temp
def safe_ensure_dirs(dirname):
  try:
    os.makedirs(dirname)
  except OSError as e:
    # Python 2 compatibility: makedirs does not support exist_ok parameter
    # Ignore error for already existing dirname as exist_ok does
    if not os.path.isdir(dirname):
      raise e


# Returns a path to EMSCRIPTEN_TEMP_DIR, creating one if it didn't exist.
def get_emscripten_temp_dir():
  global configuration, EMSCRIPTEN_TEMP_DIR
  if not EMSCRIPTEN_TEMP_DIR:
    EMSCRIPTEN_TEMP_DIR = tempfile.mkdtemp(prefix='emscripten_temp_', dir=configuration.TEMP_DIR)

    def prepare_to_clean_temp(d):
      def clean_temp():
        try_delete(d)

      atexit.register(clean_temp)
    prepare_to_clean_temp(EMSCRIPTEN_TEMP_DIR) # this global var might change later
  return EMSCRIPTEN_TEMP_DIR


def get_canonical_temp_dir(temp_dir):
  return os.path.join(temp_dir, 'emscripten_temp')


class WarningManager(object):
  warnings = {
    'ABSOLUTE_PATHS': {
      'enabled': False,  # warning about absolute-paths is disabled by default
      'printed': False,
      'message': '-I or -L of an absolute path encountered. If this is to a local system header/library, it may cause problems (local system files make sense for compiling natively on your system, but not necessarily to JavaScript).',
    },
    'SEPARATE_ASM': {
      'enabled': True,
      'printed': False,
      'message': "--separate-asm works best when compiling to HTML. Otherwise, you must yourself load the '.asm.js' file that is emitted separately, and must do so before loading the main '.js' file.",
    },
    'ALMOST_ASM': {
      'enabled': True,
      'printed': False,
      'message': 'not all asm.js optimizations are possible with ALLOW_MEMORY_GROWTH, disabling those.',
    },
  }

  @staticmethod
  def capture_warnings(cmd_args):
    for i in range(len(cmd_args)):
      if not cmd_args[i].startswith('-W'):
        continue

      # special case pre-existing warn-absolute-paths
      if cmd_args[i] == '-Wwarn-absolute-paths':
        cmd_args[i] = ''
        WarningManager.warnings['ABSOLUTE_PATHS']['enabled'] = True
      elif cmd_args[i] == '-Wno-warn-absolute-paths':
        cmd_args[i] = ''
        WarningManager.warnings['ABSOLUTE_PATHS']['enabled'] = False
      else:
        # convert to string representation of Warning
        warning_enum = cmd_args[i].replace('-Wno-', '').replace('-W', '')
        warning_enum = warning_enum.upper().replace('-', '_')

        if warning_enum in WarningManager.warnings:
          WarningManager.warnings[warning_enum]['enabled'] = not cmd_args[i].startswith('-Wno-')
          cmd_args[i] = ''

    return cmd_args

  @staticmethod
  def warn(warning_type, message=None):
    warning = WarningManager.warnings[warning_type]
    if warning['enabled'] and not warning['printed']:
      warning['printed'] = True
      logging.warning((message or warning['message']) + ' [-W' + warning_type.lower().replace('_', '-') + ']')


class Configuration(object):
  def __init__(self, environ=os.environ):
    self.DEBUG = environ.get('EMCC_DEBUG')
    if self.DEBUG == "0":
      self.DEBUG = None
    self.DEBUG_CACHE = self.DEBUG and "cache" in self.DEBUG
    self.EMSCRIPTEN_TEMP_DIR = None

    if "EMCC_TEMP_DIR" in environ:
      TEMP_DIR = environ.get("EMCC_TEMP_DIR")
    try:
      self.TEMP_DIR = TEMP_DIR
    except NameError:
      self.TEMP_DIR = find_temp_directory()
      if self.TEMP_DIR is None:
        logging.critical('TEMP_DIR not defined in ' + hint_config_file_location() + ", and could not detect a suitable directory! Please configure .emscripten to contain a variable TEMP_DIR='/path/to/temp/dir'.")
      logging.debug('TEMP_DIR not defined in ' + hint_config_file_location() + ', using ' + self.TEMP_DIR)

    if not os.path.isdir(self.TEMP_DIR):
      logging.critical("The temp directory TEMP_DIR='" + self.TEMP_DIR + "' doesn't seem to exist! Please make sure that the path is correct.")

    self.CANONICAL_TEMP_DIR = get_canonical_temp_dir(self.TEMP_DIR)

    if self.DEBUG:
      try:
        self.EMSCRIPTEN_TEMP_DIR = self.CANONICAL_TEMP_DIR
        safe_ensure_dirs(self.EMSCRIPTEN_TEMP_DIR)
      except Exception as e:
        logging.error(str(e) + 'Could not create canonical temp dir. Check definition of TEMP_DIR in ' + hint_config_file_location())

  def get_temp_files(self):
    return tempfiles.TempFiles(
      tmp=self.TEMP_DIR if not self.DEBUG else get_emscripten_temp_dir(),
      save_debug_files=os.environ.get('EMCC_DEBUG_SAVE'))


def apply_configuration():
  global configuration, DEBUG, EMSCRIPTEN_TEMP_DIR, DEBUG_CACHE, CANONICAL_TEMP_DIR, TEMP_DIR
  configuration = Configuration()
  DEBUG = configuration.DEBUG
  EMSCRIPTEN_TEMP_DIR = configuration.EMSCRIPTEN_TEMP_DIR
  DEBUG_CACHE = configuration.DEBUG_CACHE
  CANONICAL_TEMP_DIR = configuration.CANONICAL_TEMP_DIR
  TEMP_DIR = configuration.TEMP_DIR


def set_logging():
  logging.basicConfig(format='%(levelname)-8s %(name)s: %(message)s') # can add  %(asctime)s  to see timestamps
  logger = logging.getLogger()
  logger.setLevel(logging.DEBUG if os.environ.get('EMCC_DEBUG') else logging.INFO)


apply_configuration()
set_logging()

# EM_CONFIG stuff
if JS_ENGINES is None:
  if JS_ENGINE is None:
    raise 'ERROR: %s does not seem to have JS_ENGINES or JS_ENGINE set up' % EM_CONFIG
  else:
    JS_ENGINES = [JS_ENGINE]

if CLOSURE_COMPILER is None:
  CLOSURE_COMPILER = path_from_root('third_party', 'closure-compiler', 'compiler.jar')

if PYTHON is None:
  logging.debug('PYTHON not defined in ' + hint_config_file_location() + ', using "%s"' % (sys.executable,))
  PYTHON = sys.executable

if JAVA is None:
  logging.debug('JAVA not defined in ' + hint_config_file_location() + ', using "java"')
  JAVA = 'java'

# Additional compiler options

# Target choice.
ASM_JS_TARGET = 'asmjs-unknown-emscripten'
WASM_TARGET = 'wasm32-unknown-unknown-wasm'


def check_vanilla():
  global LLVM_TARGET
  # if the env var tells us what to do, do that
  if 'EMCC_WASM_BACKEND' in os.environ:
    if os.environ['EMCC_WASM_BACKEND'] != '0':
      logging.debug('EMCC_WASM_BACKEND tells us to use wasm backend')
      LLVM_TARGET = WASM_TARGET
    else:
      logging.debug('EMCC_WASM_BACKEND tells us to use asm.js backend')
      LLVM_TARGET = ASM_JS_TARGET
  else:
    # if we are using vanilla LLVM, i.e. we don't have our asm.js backend, then we
    # must use wasm (or at least try to). to know that, we have to run llc to
    # see which backends it has. we cache this result.
    temp_cache = cache.Cache(use_subdir=False)

    def check_vanilla():
      logging.debug('testing for asm.js target, because if not present (i.e. this is plain vanilla llvm, not emscripten fastcomp), we will use the wasm target instead (set EMCC_WASM_BACKEND to skip this check)')
      targets = get_llc_targets()
      return has_wasm_target(targets) and not has_asm_js_target(targets)

    def get_vanilla_file():
      saved_file = os.path.join(temp_cache.dirname, 'is_vanilla.txt')
      open(saved_file, 'w').write(('1' if check_vanilla() else '0') + ':' + LLVM_ROOT)
      return saved_file

    is_vanilla_file = temp_cache.get('is_vanilla', get_vanilla_file, extension='.txt')
    if CONFIG_FILE and os.stat(CONFIG_FILE).st_mtime > os.stat(is_vanilla_file).st_mtime:
      logging.debug('config file changed since we checked vanilla; re-checking')
      is_vanilla_file = temp_cache.get('is_vanilla', get_vanilla_file, extension='.txt', force=True)
    try:
      contents = open(is_vanilla_file).read()
      middle = contents.index(':')
      is_vanilla = int(contents[:middle])
      llvm_used = contents[middle + 1:]
      if llvm_used != LLVM_ROOT:
        logging.debug('regenerating vanilla check since other llvm')
        temp_cache.get('is_vanilla', get_vanilla_file, extension='.txt', force=True)
        is_vanilla = check_vanilla()
    except Exception as e:
      logging.debug('failed to use vanilla file, will re-check: ' + str(e))
      is_vanilla = check_vanilla()
    temp_cache = None
    if is_vanilla:
      logging.debug('check tells us to use wasm backend')
      LLVM_TARGET = WASM_TARGET
    else:
      logging.debug('check tells us to use asm.js backend')
      LLVM_TARGET = ASM_JS_TARGET


check_vanilla()


def get_llvm_target():
  assert LLVM_TARGET is not None
  return LLVM_TARGET


# Set the LIBCPP ABI version to at least 2 so that we get nicely aligned string
# data and other nice fixes.
COMPILER_OPTS += [# '-fno-threadsafe-statics', # disabled due to issue 1289
                  '-target', get_llvm_target(),
                  '-D__EMSCRIPTEN_major__=' + str(EMSCRIPTEN_VERSION_MAJOR),
                  '-D__EMSCRIPTEN_minor__=' + str(EMSCRIPTEN_VERSION_MINOR),
                  '-D__EMSCRIPTEN_tiny__=' + str(EMSCRIPTEN_VERSION_TINY),
                  '-D_LIBCPP_ABI_VERSION=2']

if get_llvm_target() == WASM_TARGET:
  # wasm target does not automatically define emscripten stuff, so do it here.
  COMPILER_OPTS += ['-D__EMSCRIPTEN__',
                    '-Dunix',
                    '-D__unix',
                    '-D__unix__']

# Changes to default clang behavior

# Implicit functions can cause horribly confusing function pointer type errors, see #2175
# If your codebase really needs them - very unrecommended! - you can disable the error with
#   -Wno-error=implicit-function-declaration
# or disable even a warning about it with
#   -Wno-implicit-function-declaration
COMPILER_OPTS += ['-Werror=implicit-function-declaration']

USE_EMSDK = not os.environ.get('EMMAKEN_NO_SDK')

if USE_EMSDK:
  # Disable system C and C++ include directories, and add our own (using -idirafter so they are last, like system dirs, which
  # allows projects to override them)
  C_INCLUDE_PATHS = [
    path_from_root('system', 'include', 'compat'),
    path_from_root('system', 'include'),
    path_from_root('system', 'include', 'SSE'),
    path_from_root('system', 'include', 'libc'),
    path_from_root('system', 'lib', 'libc', 'musl', 'arch', 'emscripten'),
    path_from_root('system', 'local', 'include')
  ]

  CXX_INCLUDE_PATHS = [
    path_from_root('system', 'include', 'libcxx'),
    path_from_root('system', 'lib', 'libcxxabi', 'include')
  ]

  C_OPTS = ['-nostdinc', '-Xclang', '-nobuiltininc', '-Xclang', '-nostdsysteminc']

  def include_directive(paths):
    result = []
    for path in paths:
      result += ['-Xclang', '-isystem' + path]
    return result

  # libcxx include paths must be defined before libc's include paths otherwise libcxx will not build
  EMSDK_OPTS = C_OPTS + include_directive(CXX_INCLUDE_PATHS) + include_directive(C_INCLUDE_PATHS)

  EMSDK_CXX_OPTS = []
  COMPILER_OPTS += EMSDK_OPTS
else:
  EMSDK_OPTS = []
  EMSDK_CXX_OPTS = []

# Engine tweaks

try:
  if SPIDERMONKEY_ENGINE:
    new_spidermonkey = SPIDERMONKEY_ENGINE
    if '-w' not in str(new_spidermonkey):
      new_spidermonkey += ['-w']
    SPIDERMONKEY_ENGINE = fix_js_engine(SPIDERMONKEY_ENGINE, new_spidermonkey)
except NameError:
  pass

# If we have 'env', we should use that to find python, because |python| may fail while |env python| may work
# (For example, if system python is 3.x while we need 2.x, and env gives 2.x if told to do so.)
ENV_PREFIX = []
if not WINDOWS:
  try:
    assert 'Python' in run_process(['env', 'python', '-V'], stdout=PIPE, stderr=STDOUT).stdout
    ENV_PREFIX = ['env']
  except:
    pass


# Utilities
def make_js_command(filename, engine=None, *args):
  if engine is None:
    engine = JS_ENGINES[0]
  return jsrun.make_command(filename, engine, *args)


def run_js(filename, engine=None, *args, **kw):
  if engine is None:
    engine = JS_ENGINES[0]
  return jsrun.run_js(filename, engine, *args, **kw)


def to_cc(cxx):
  # By default, LLVM_GCC and CLANG are really the C++ versions. This gets an explicit C version
  return cxx.replace('clang++', 'clang').replace('g++', 'gcc')


def line_splitter(data):
  """Silly little tool to split JSON arrays over many lines."""

  out = ''
  counter = 0

  for i in range(len(data)):
    out += data[i]
    if data[i] == ' ' and counter > 60:
      out += '\n'
      counter = 0
    else:
      counter += 1

  return out


def limit_size(string, MAX=800 * 20):
  if len(string) < MAX:
    return string
  return string[0:MAX / 2] + '\n[..]\n' + string[-MAX / 2:]


def read_pgo_data(filename):
  '''
    Reads the output of PGO and generates proper information for CORRECT_* == 2 's *_LINES options
  '''
  signs_lines = []
  overflows_lines = []

  for line in open(filename, 'r'):
    try:
      if line.rstrip() == '':
        continue
      if '%0 failures' in line:
        continue
      left, right = line.split(' : ')
      signature = left.split('|')[1]
      if 'Sign' in left:
        signs_lines.append(signature)
      elif 'Overflow' in left:
        overflows_lines.append(signature)
    except:
      pass

  return {
    'signs_lines': signs_lines,
    'overflows_lines': overflows_lines
  }


def unique_ordered(values):
  """return a list of unique values in an input list, without changing order
  (list(set(.)) would change order randomly).
  """
  seen = set()

  def check(value):
    if value in seen:
      return False
    seen.add(value)
    return True

  return list(filter(check, values))


def expand_response(data):
  if type(data) == str and data[0] == '@':
    return json.loads(open(data[1:]).read())
  return data


def expand_byte_size_suffixes(value):
  """Given a string with arithmetic and/or KB/MB size suffixes, such as
  "1024*1024" or "32MB", computes how many bytes that is and returns it as an
  integer.
  """
  value = value.lower().replace('tb', '*1024*1024*1024*1024').replace('gb', '*1024*1024*1024').replace('mb', '*1024*1024').replace('kb', '*1024').replace('b', '')
  try:
    return eval(value)
  except:
    raise Exception("Invalid byte size, valid suffixes: KB, MB, GB, TB")


# Settings. A global singleton. Not pretty, but nicer than passing |, settings| everywhere
class SettingsManager(object):
  class __impl(object):
    attrs = {}

    def __init__(self):
      self.reset()

    @classmethod
    def reset(self):
      self.attrs = {'QUANTUM_SIZE': 4}
      self.load()

    # Given some emcc-type args (-O3, -s X=Y, etc.), fill Settings with the right settings
    @classmethod
    def load(self, args=[]):
      # Load the JS defaults into python
      settings = open(path_from_root('src', 'settings.js')).read().replace('//', '#')
      settings = re.sub(r'var ([\w\d]+)', r'self.attrs["\1"]', settings)
      exec(settings)

      # Apply additional settings. First -O, then -s
      for arg in args:
        if arg.startswith('-O'):
          v = arg[2]
          shrink = 0
          if v in ['s', 'z']:
            shrink = 1 if v == 's' else 2
            v = '2'
          level = eval(v)
          self.apply_opt_level(level, shrink)
      for i in range(len(args)):
        if args[i] == '-s':
          declare = re.sub(r'([\w\d]+)\s*=\s*(.+)', r'self.attrs["\1"]=\2;', args[i + 1])
          exec(declare)

      if get_llvm_target() == WASM_TARGET:
        self.attrs['WASM_BACKEND'] = 1

    # Transforms the Settings information into emcc-compatible args (-s X=Y, etc.). Basically
    # the reverse of load_settings, except for -Ox which is relevant there but not here
    @classmethod
    def serialize(self):
      ret = []
      for key, value in self.attrs.items():
        if key == key.upper():  # this is a hack. all of our settings are ALL_CAPS, python internals are not
          jsoned = json.dumps(value, sort_keys=True)
          ret += ['-s', key + '=' + jsoned]
      return ret

    @classmethod
    def to_dict(self):
      return self.attrs.copy()

    @classmethod
    def copy(self, values):
      self.attrs = values

    @classmethod
    def apply_opt_level(self, opt_level, shrink_level=0, noisy=False):
      if opt_level >= 1:
        self.attrs['ASM_JS'] = 1
        self.attrs['ASSERTIONS'] = 0
        self.attrs['ALIASING_FUNCTION_POINTERS'] = 1
      if shrink_level >= 2:
        self.attrs['EVAL_CTORS'] = 1

    def __getattr__(self, attr):
      if attr in self.attrs:
        return self.attrs[attr]
      else:
        raise AttributeError

    def __setattr__(self, attr, value):
      if attr not in self.attrs:
        import difflib
        logging.warning('''Assigning a non-existent settings attribute "%s"''' % attr)
        suggestions = ', '.join(difflib.get_close_matches(attr, list(self.attrs.keys())))
        if suggestions:
          logging.warning(''' - did you mean one of %s?''' % suggestions)
        logging.warning(''' - perhaps a typo in emcc's  -s X=Y  notation?''')
        logging.warning(''' - (see src/settings.js for valid values)''')
      self.attrs[attr] = value

    @classmethod
    def get(self, key):
      return self.attrs.get(key)

    @classmethod
    def __getitem__(self, key):
      return self.attrs[key]

  __instance = None

  @staticmethod
  def instance():
    if SettingsManager.__instance is None:
      SettingsManager.__instance = SettingsManager.__impl()
    return SettingsManager.__instance

  def __getattr__(self, attr):
    return getattr(self.instance(), attr)

  def __setattr__(self, attr, value):
    return setattr(self.instance(), attr, value)

  def get(self, key):
    return self.instance().get(key)

  def __getitem__(self, key):
    return self.instance()[key]


def verify_settings():
  if Settings.WASM_BACKEND:
    if not Settings.WASM:
      # TODO(sbc): Make this into a hard error.  We still have a few places that
      # pass WASM=0 before we can do this (at least Platform/Emscripten.cmake and
      # generate_struct_info).
      logging.debug('emcc: WASM_BACKEND is not compatible with asmjs (WASM=0), forcing WASM=1')
      Settings.WASM = 1

    if not BINARYEN_ROOT:
      exit_with_error('emcc: BINARYEN_ROOT must be set in the .emscripten config'
                      ' when using the LLVM wasm backend')

    if Settings.CYBERDWARF:
      exit_with_error('emcc: CYBERDWARF is not supported by the LLVM wasm backend')

    if Settings.EMTERPRETIFY:
      exit_with_error('emcc: EMTERPRETIFY is not is not supported by the LLVM wasm backend')


Settings = SettingsManager()
verify_settings()


# llvm-ar appears to just use basenames inside archives. as a result, files with the same basename
# will trample each other when we extract them. to help warn of such situations, we warn if there
# are duplicate entries in the archive
def warn_if_duplicate_entries(archive_contents, archive_filename_hint=''):
  if len(archive_contents) != len(set(archive_contents)):
    logging.warning('loading from archive %s, which has duplicate entries (files with identical base names). this is dangerous as only the last will be taken into account, and you may see surprising undefined symbols later. you should rename source files to avoid this problem (or avoid .a archives, and just link bitcode together to form libraries for later linking)' % archive_filename_hint)
    warned = set()
    for i in range(len(archive_contents)):
      curr = archive_contents[i]
      if curr not in warned and curr in archive_contents[i + 1:]:
        logging.warning('   duplicate: %s' % curr)
        warned.add(curr)


# N.B. This function creates a temporary directory specified by the 'dir' field in the returned dictionary. Caller
# is responsible for cleaning up those files after done.
def extract_archive_contents(f):
  try:
    cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp('_archive_contents', 'emscripten_temp_')
    safe_ensure_dirs(temp_dir)
    os.chdir(temp_dir)
    contents = [x for x in run_process([LLVM_AR, 't', f], stdout=PIPE).stdout.split('\n') if len(x)]
    warn_if_duplicate_entries(contents, f)
    if len(contents) == 0:
      logging.debug('Archive %s appears to be empty (recommendation: link an .so instead of .a)' % f)
      return {
        'returncode': 0,
        'dir': temp_dir,
        'files': []
      }

    # We are about to ask llvm-ar to extract all the files in the .a archive file, but
    # it will silently fail if the directory for the file does not exist, so make all the necessary directories
    for content in contents:
      dirname = os.path.dirname(content)
      if dirname:
        safe_ensure_dirs(dirname)
    proc = run_process([LLVM_AR, 'xo', f], stdout=PIPE, stderr=STDOUT)
    # if absolute paths, files will appear there. otherwise, in this directory
    contents = [os.path.abspath(c) for c in contents]
    nonexisting_contents = [x for x in contents if not os.path.exists(x)]
    if len(nonexisting_contents):
      raise Exception('llvm-ar failed to extract file(s) ' + str(nonexisting_contents) + ' from archive file ' + f + '! Error:' + str(proc.stdout))

    return {
      'returncode': proc.returncode,
      'dir': temp_dir,
      'files': contents
    }
  except Exception as e:
    print('extract archive contents( ' + str(f) + ') failed with error: ' + str(e), file=sys.stderr)
  finally:
    os.chdir(cwd)

  return {
    'returncode': 1,
    'dir': None,
    'files': []
  }


class ObjectFileInfo(object):
  def __init__(self, returncode, output, defs=set(), undefs=set(), commons=set()):
    self.returncode = returncode
    self.output = output
    self.defs = defs
    self.undefs = undefs
    self.commons = commons

  def is_valid_for_nm(self):
    return self.returncode == 0


# Due to a python pickling issue, the following two functions must be at top
# level, or multiprocessing pool spawn won't find them.
def g_llvm_nm_uncached(filename):
  return Building.llvm_nm_uncached(filename)


def g_multiprocessing_initializer(*args):
  for item in args:
    (key, value) = item.split('=', 1)
    if key == 'EMCC_POOL_CWD':
      os.chdir(value)
    else:
      os.environ[key] = value


#  Building
class Building(object):
  COMPILER = CLANG
  LLVM_OPTS = False
  COMPILER_TEST_OPTS = [] # For use of the test runner
  JS_ENGINE_OVERRIDE = None # Used to pass the JS engine override from runner.py -> test_benchmark.py
  multiprocessing_pool = None

  @staticmethod
  def get_num_cores():
    return int(os.environ.get('EMCC_CORES') or multiprocessing.cpu_count())

  # Multiprocessing pools are very slow to build up and tear down, and having several pools throughout
  # the application has a problem of overallocating child processes. Therefore maintain a single
  # centralized pool that is shared between all pooled task invocations.
  @staticmethod
  def get_multiprocessing_pool():
    if not Building.multiprocessing_pool:
      cores = Building.get_num_cores()

      # If running with one core only, create a mock instance of a pool that does not
      # actually spawn any new subprocesses. Very useful for internal debugging.
      if cores == 1:
        class FakeMultiprocessor(object):
          def map(self, func, tasks):
            results = []
            for t in tasks:
              results += [func(t)]
            return results
        Building.multiprocessing_pool = FakeMultiprocessor()
      else:
        child_env = [
          # Multiprocessing pool children must have their current working
          # directory set to a safe path that is guaranteed not to die in
          # between of executing commands, or otherwise the pool children will
          # have trouble spawning subprocesses of their own.
          'EMCC_POOL_CWD=' + path_from_root(),
          # Multiprocessing pool children need to avoid all calling
          # check_vanilla() again and again, otherwise the compiler can deadlock
          # when building system libs, because the multiprocess parent can have
          # the Emscripten cache directory locked for write access, and the
          # EMCC_WASM_BACKEND check also requires locked access to the cache,
          # which the multiprocess children would not get.
          'EMCC_WASM_BACKEND=%s' % Settings.WASM_BACKEND,
          # Multiprocessing pool children can't spawn their own linear number of
          # children, that could cause a quadratic amount of spawned processes.
          'EMCC_CORES=1'
        ]
        Building.multiprocessing_pool = multiprocessing.Pool(processes=cores, initializer=g_multiprocessing_initializer, initargs=child_env)

        def close_multiprocessing_pool():
          try:
            # Shut down the pool explicitly, because leaving that for Python to do at process shutdown is buggy and can generate
            # noisy "WindowsError: [Error 5] Access is denied" spam which is not fatal.
            Building.multiprocessing_pool.terminate()
            Building.multiprocessing_pool.join()
            Building.multiprocessing_pool = None
          except OSError as e:
            # Mute the "WindowsError: [Error 5] Access is denied" errors, raise all others through
            if not (sys.platform.startswith('win') and isinstance(e, WindowsError) and e.winerror == 5):
              raise
        atexit.register(close_multiprocessing_pool)

    return Building.multiprocessing_pool

  # When creating environment variables for Makefiles to execute, we need to doublequote the commands if they have spaces in them..
  @staticmethod
  def doublequote_spaces(arg):
    arg = arg[:] # Operate on a copy of the input string/list
    if isinstance(arg, list):
      for i in range(len(arg)):
        arg[i] = Building.doublequote_spaces(arg[i])
      return arg

    if ' ' in arg and (not (arg.startswith('"') and arg.endswith('"'))) and (not (arg.startswith("'") and arg.endswith("'"))):
      return '"' + arg.replace('"', '\\"') + '"'

    return arg

  # .. but for Popen, we cannot have doublequotes, so provide functionality to remove them when needed.
  @staticmethod
  def remove_quotes(arg):
    arg = arg[:] # Operate on a copy of the input string/list
    if isinstance(arg, list):
      for i in range(len(arg)):
        arg[i] = Building.remove_quotes(arg[i])
      return arg

    if arg.startswith('"') and arg.endswith('"'):
      return arg[1:-1].replace('\\"', '"')
    elif arg.startswith("'") and arg.endswith("'"):
      return arg[1:-1].replace("\\'", "'")
    else:
      return arg

  @staticmethod
  def get_building_env(native=False, doublequote_commands=False):
    def nop(arg):
      return arg
    quote = Building.doublequote_spaces if doublequote_commands else nop
    env = os.environ.copy()
    if native:
      env['CC'] = quote(CLANG_CC)
      env['CXX'] = quote(CLANG_CPP)
      env['LD'] = quote(CLANG)
      env['CFLAGS'] = '-O2 -fno-math-errno'
      # get a non-native one, and see if we have some of its effects - remove them if so
      non_native = Building.get_building_env()
      # the ones that a non-native would modify
      EMSCRIPTEN_MODIFIES = ['LDSHARED', 'AR', 'CROSS_COMPILE', 'NM', 'RANLIB']
      for dangerous in EMSCRIPTEN_MODIFIES:
        if env.get(dangerous) and env.get(dangerous) == non_native.get(dangerous):
          del env[dangerous] # better to delete it than leave it, as the non-native one is definitely wrong
      return env
    # point CC etc. to the em* tools.
    # on windows, we must specify python explicitly. on other platforms, we prefer
    # not to, as some configure scripts expect e.g. CC to be a literal executable
    # (but "python emcc.py" is not a file that exists).
    # note that we point to emcc etc. here, without a suffix, instead of to
    # emcc.py etc. The unsuffixed versions have the python_selector logic that can
    # pick the right version as needed (which is not crucial right now as we support
    # both 2 and 3, but eventually we may be 3-only).
    env['CC'] = quote(unsuffixed(EMCC)) if not WINDOWS else 'python %s' % quote(EMCC)
    env['CXX'] = quote(unsuffixed(EMXX)) if not WINDOWS else 'python %s' % quote(EMXX)
    env['AR'] = quote(unsuffixed(EMAR)) if not WINDOWS else 'python %s' % quote(EMAR)
    env['LD'] = quote(unsuffixed(EMCC)) if not WINDOWS else 'python %s' % quote(EMCC)
    env['NM'] = quote(LLVM_NM)
    env['LDSHARED'] = quote(unsuffixed(EMCC)) if not WINDOWS else 'python %s' % quote(EMCC)
    env['RANLIB'] = quote(unsuffixed(EMRANLIB)) if not WINDOWS else 'python %s' % quote(EMRANLIB)
    env['EMMAKEN_COMPILER'] = quote(Building.COMPILER)
    env['EMSCRIPTEN_TOOLS'] = path_from_root('tools')
    env['CFLAGS'] = env['EMMAKEN_CFLAGS'] = ' '.join(Building.COMPILER_TEST_OPTS)
    env['HOST_CC'] = quote(CLANG_CC)
    env['HOST_CXX'] = quote(CLANG_CPP)
    env['HOST_CFLAGS'] = "-W" # if set to nothing, CFLAGS is used, which we don't want
    env['HOST_CXXFLAGS'] = "-W" # if set to nothing, CXXFLAGS is used, which we don't want
    env['PKG_CONFIG_LIBDIR'] = path_from_root('system', 'local', 'lib', 'pkgconfig') + os.path.pathsep + path_from_root('system', 'lib', 'pkgconfig')
    env['PKG_CONFIG_PATH'] = os.environ.get('EM_PKG_CONFIG_PATH', '')
    env['EMSCRIPTEN'] = path_from_root()
    env['PATH'] = path_from_root('system', 'bin') + os.pathsep + env['PATH']
    env['CROSS_COMPILE'] = path_from_root('em') # produces /path/to/emscripten/em , which then can have 'cc', 'ar', etc appended to it
    return env

  # if we are in emmake mode, i.e., we changed the env to run emcc etc., then show the message and abort
  @staticmethod
  def ensure_no_emmake(message):
    non_native = Building.get_building_env()
    if os.environ.get('CC') == non_native.get('CC'):
      # the environment CC is the one we change to when forcing our em* tools
      exit_with_error(message)

  # Finds the given executable 'program' in PATH. Operates like the Unix tool 'which'.
  @staticmethod
  def which(program):
    def is_exe(fpath):
      return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    if os.path.isabs(program):
      if os.path.isfile(program):
        return program

      if WINDOWS:
        for suffix in ['.exe', '.cmd', '.bat']:
          if is_exe(program + suffix):
            return program + suffix

    fpath, fname = os.path.split(program)
    if fpath:
      if is_exe(program):
        return program
    else:
      for path in os.environ["PATH"].split(os.pathsep):
        path = path.strip('"')
        exe_file = os.path.join(path, program)
        if is_exe(exe_file):
          return exe_file
        if WINDOWS:
          for suffix in ('.exe', '.cmd', '.bat'):
            if is_exe(exe_file + suffix):
              return exe_file + suffix

    return None

  # Returns a clone of the given environment with all directories that contain sh.exe removed from the PATH.
  # Used to work around CMake limitation with MinGW Makefiles, where sh.exe is not allowed to be present.
  @staticmethod
  def remove_sh_exe_from_path(env):
    env = env.copy()
    if not WINDOWS:
      return env
    path = env['PATH'].split(';')
    path = [p for p in path if not os.path.exists(os.path.join(p, 'sh.exe'))]
    env['PATH'] = ';'.join(path)
    return env

  @staticmethod
  def handle_CMake_toolchain(args, env):

    def has_substr(array, substr):
      for arg in array:
        if substr in arg:
          return True
      return False

    # Append the Emscripten toolchain file if the user didn't specify one.
    if not has_substr(args, '-DCMAKE_TOOLCHAIN_FILE'):
      args.append('-DCMAKE_TOOLCHAIN_FILE=' + path_from_root('cmake', 'Modules', 'Platform', 'Emscripten.cmake'))

    # On Windows specify MinGW Makefiles if we have MinGW and no other toolchain was specified, to avoid CMake
    # pulling in a native Visual Studio, or Unix Makefiles.
    if WINDOWS and '-G' not in args and Building.which('mingw32-make'):
      args += ['-G', 'MinGW Makefiles']

    # CMake has a requirement that it wants sh.exe off PATH if MinGW Makefiles is being used. This happens quite often,
    # so do this automatically on behalf of the user. See http://www.cmake.org/Wiki/CMake_MinGW_Compiler_Issues
    if WINDOWS and 'MinGW Makefiles' in args:
      env = Building.remove_sh_exe_from_path(env)

    return (args, env)

  @staticmethod
  def configure(args, stdout=None, stderr=None, env=None):
    if not args:
      return
    if env is None:
      env = Building.get_building_env()
    if 'cmake' in args[0]:
      # Note: EMMAKEN_JUST_CONFIGURE shall not be enabled when configuring with CMake. This is because CMake
      #       does expect to be able to do config-time builds with emcc.
      args, env = Building.handle_CMake_toolchain(args, env)
    else:
      # When we configure via a ./configure script, don't do config-time compilation with emcc, but instead
      # do builds natively with Clang. This is a heuristic emulation that may or may not work.
      env['EMMAKEN_JUST_CONFIGURE'] = '1'
    try:
      if EM_BUILD_VERBOSE_LEVEL >= 3:
        print('configure: ' + str(args), file=sys.stderr)
      if EM_BUILD_VERBOSE_LEVEL >= 2:
        stdout = None
      if EM_BUILD_VERBOSE_LEVEL >= 1:
        stderr = None
      res = run_process(args, check=False, stdout=stdout, stderr=stderr, env=env)
    except Exception:
      logging.error('Error running configure: "%s"' % ' '.join(args))
      raise
    if 'EMMAKEN_JUST_CONFIGURE' in env:
      del env['EMMAKEN_JUST_CONFIGURE']
    if res.returncode is not 0:
      logging.error('Configure step failed with non-zero return code: %s.  Command line: %s at %s' % (res.returncode, ' '.join(args), os.getcwd()))
      raise subprocess.CalledProcessError(cmd=args, returncode=res.returncode)

  @staticmethod
  def make(args, stdout=None, stderr=None, env=None):
    if env is None:
      env = Building.get_building_env()
    if not args:
      exit_with_error('Executable to run not specified.')
    # args += ['VERBOSE=1']

    # On Windows prefer building with mingw32-make instead of make, if it exists.
    if WINDOWS:
      if args[0] == 'make':
        mingw32_make = Building.which('mingw32-make')
        if mingw32_make:
          args[0] = mingw32_make

      if 'mingw32-make' in args[0]:
        env = Building.remove_sh_exe_from_path(env)

    try:
      # On Windows, run the execution through shell to get PATH expansion and executable extension lookup, e.g. 'sdl2-config' will match with 'sdl2-config.bat' in PATH.
      if EM_BUILD_VERBOSE_LEVEL >= 3:
        print('make: ' + str(args), file=sys.stderr)
      if EM_BUILD_VERBOSE_LEVEL >= 2:
        stdout = None
      if EM_BUILD_VERBOSE_LEVEL >= 1:
        stderr = None
      res = run_process(args, stdout=stdout, stderr=stderr, env=env, shell=WINDOWS, check=False)
    except Exception:
      logging.error('Error running make: "%s"' % ' '.join(args))
      raise
    if res.returncode != 0:
      raise subprocess.CalledProcessError(cmd=args, returncode=res.returncode)

  @staticmethod
  def build_library(name,
                    build_dir,
                    output_dir,
                    generated_libs,
                    configure=['sh', './configure'],
                    configure_args=[],
                    make=['make'],
                    make_args='help',
                    cache=None,
                    cache_name=None,
                    copy_project=False,
                    env_init={},
                    source_dir=None,
                    native=False):
    """Build a library into a .bc file. We build the .bc file once and cache it
    for all our tests. (We cache in memory since the test directory is destroyed
    and recreated for each test. Note that we cache separately for different
    compilers).  This cache is just during the test runner. There is a different
    concept of caching as well, see |Cache|.
    """

    if type(generated_libs) is not list:
      generated_libs = [generated_libs]
    if source_dir is None:
      source_dir = path_from_root('tests', name.replace('_native', ''))
    if make_args == 'help':
      make_args = ['-j', str(multiprocessing.cpu_count())]

    temp_dir = build_dir
    if copy_project:
      project_dir = os.path.join(temp_dir, name)
      if os.path.exists(project_dir):
        shutil.rmtree(project_dir)
      shutil.copytree(source_dir, project_dir) # Useful in debugging sometimes to comment this out, and two lines above
    else:
      project_dir = build_dir
    try:
      old_dir = os.getcwd()
    except:
      old_dir = None
    os.chdir(project_dir)
    generated_libs = [os.path.join(project_dir, lib) for lib in generated_libs]
    # for lib in generated_libs:
    #   try:
    #     os.unlink(lib) # make sure compilation completed successfully
    #   except:
    #     pass
    env = Building.get_building_env(native, True)
    for k, v in env_init.items():
      env[k] = v
    if configure:
      # Useful in debugging sometimes to comment this out (and the lines below
      # up to and including the |link| call)
      if EM_BUILD_VERBOSE_LEVEL < 2:
        stdout = open(os.path.join(project_dir, 'configure_out'), 'w')
      else:
        stdout = None
      if EM_BUILD_VERBOSE_LEVEL < 1:
        stderr = open(os.path.join(project_dir, 'configure_err'), 'w')
      else:
        stderr = None
      try:
        Building.configure(configure + configure_args, env=env, stdout=stdout, stderr=stderr)
      except subprocess.CalledProcessError as e:
        pass # Ignore exit code != 0

    def open_make_out(i, mode='r'):
      return open(os.path.join(project_dir, 'make_out' + str(i)), mode)

    def open_make_err(i, mode='r'):
      return open(os.path.join(project_dir, 'make_err' + str(i)), mode)

    if EM_BUILD_VERBOSE_LEVEL >= 3:
      make_args += ['VERBOSE=1']

    # FIXME: Sad workaround for some build systems that need to be run twice to succeed (e.g. poppler)
    for i in range(2):
      with open_make_out(i, 'w') as make_out:
        with open_make_err(i, 'w') as make_err:
          stdout = make_out if EM_BUILD_VERBOSE_LEVEL < 2 else None
          stderr = make_err if EM_BUILD_VERBOSE_LEVEL < 1 else None
          try:
            Building.make(make + make_args, stdout=stdout, stderr=stderr, env=env)
          except subprocess.CalledProcessError as e:
            pass # Ignore exit code != 0
      try:
        if cache is not None:
          cache[cache_name] = []
          for f in generated_libs:
            basename = os.path.basename(f)
            cache[cache_name].append((basename, open(f, 'rb').read()))
        break
      except Exception as e:
        if i > 0:
          if EM_BUILD_VERBOSE_LEVEL == 0:
            # Due to the ugly hack above our best guess is to output the first run
            with open_make_err(0) as ferr:
              for line in ferr:
                sys.stderr.write(line)
          raise Exception('could not build library ' + name + ' due to exception ' + str(e))
    if old_dir:
      os.chdir(old_dir)
    return generated_libs

  @staticmethod
  def make_paths_absolute(f):
    if f.startswith('-'):  # skip flags
      return f
    else:
      return os.path.abspath(f)

  # Runs llvm-nm in parallel for the given list of files.
  # The results are populated in Building.uninternal_nm_cache
  # multiprocessing_pool: An existing multiprocessing pool to reuse for the operation, or None
  # to have the function allocate its own.
  @staticmethod
  def parallel_llvm_nm(files):
    with ToolchainProfiler.profile_block('parallel_llvm_nm'):
      pool = Building.get_multiprocessing_pool()
      object_contents = pool.map(g_llvm_nm_uncached, files)

      for i in range(len(files)):
        if object_contents[i].returncode != 0:
          logging.debug('llvm-nm failed on file ' + files[i] + ': return code ' + str(object_contents[i].returncode) + ', error: ' + object_contents[i].output)
        Building.uninternal_nm_cache[files[i]] = object_contents[i]
      return object_contents

  @staticmethod
  def read_link_inputs(files):
    with ToolchainProfiler.profile_block('read_link_inputs'):
      # Before performing the link, we need to look at each input file to determine which symbols
      # each of them provides. Do this in multiple parallel processes.
      archive_names = [] # .a files passed in to the command line to the link
      object_names = [] # .o/.bc files passed in to the command line to the link
      for f in files:
        absolute_path_f = Building.make_paths_absolute(f)

        if absolute_path_f not in Building.ar_contents and Building.is_ar(absolute_path_f):
          archive_names.append(absolute_path_f)
        elif absolute_path_f not in Building.uninternal_nm_cache and Building.is_bitcode(absolute_path_f):
          object_names.append(absolute_path_f)

      # Archives contain objects, so process all archives first in parallel to obtain the object files in them.
      pool = Building.get_multiprocessing_pool()
      object_names_in_archives = pool.map(extract_archive_contents, archive_names)

      def clean_temporary_archive_contents_directory(directory):
        def clean_at_exit():
          try_delete(directory)
        if directory:
          atexit.register(clean_at_exit)

      for n in range(len(archive_names)):
        if object_names_in_archives[n]['returncode'] != 0:
          raise Exception('llvm-ar failed on archive ' + archive_names[n] + '!')
        Building.ar_contents[archive_names[n]] = object_names_in_archives[n]['files']
        clean_temporary_archive_contents_directory(object_names_in_archives[n]['dir'])

      for o in object_names_in_archives:
        for f in o['files']:
          if f not in Building.uninternal_nm_cache:
            object_names.append(f)

      # Next, extract symbols from all object files (either standalone or inside archives we just extracted)
      # The results are not used here directly, but populated to llvm-nm cache structure.
      Building.parallel_llvm_nm(object_names)

  @staticmethod
  def llvm_backend_args():
    args = ['-thread-model=single'] # no threads support in backend, tell llc to not emit atomics
    # disable slow and relatively unimportant optimization passes
    args += ['-combiner-global-alias-analysis=false']

    # asm.js-style exception handling
    if Settings.DISABLE_EXCEPTION_CATCHING != 1:
      args += ['-enable-emscripten-cxx-exceptions']
    if Settings.DISABLE_EXCEPTION_CATCHING == 2:
      whitelist = ','.join(Settings.EXCEPTION_CATCHING_WHITELIST or ['__fake'])
      args += ['-emscripten-cxx-exceptions-whitelist=' + whitelist]

    # asm.js-style setjmp/longjmp handling
    args += ['-enable-emscripten-sjlj']
    return args

  @staticmethod
  def link_lld(files, target, opts=[], lto_level=0):
    cmd = [
        WASM_LD,
        '-z',
        'stack-size=%s' % Settings.TOTAL_STACK,
        '--global-base=%s' % Settings.GLOBAL_BASE,
        '--initial-memory=%d' % Settings.TOTAL_MEMORY,
        '-o',
        target,
        '--no-entry',
        '--allow-undefined',
        '--import-memory',
        '--export',
        '__wasm_call_ctors',
        '--lto-O%d' % lto_level,
    ] + files

    if Settings.WASM_MEM_MAX != -1:
      cmd.append('--max-memory=%d' % Settings.WASM_MEM_MAX)
    elif not Settings.ALLOW_MEMORY_GROWTH:
      cmd.append('--max-memory=%d' % Settings.TOTAL_MEMORY)

    for a in Building.llvm_backend_args():
      cmd += ['-mllvm', a]

    # emscripten-wasm-finalize currently depends on the presence of debug
    # symbols for renaming of the __invoke symbols
    # TODO(sbc): Re-enable once emscripten-wasm-finalize is fixed or we
    # no longer need to rename these symbols.
    # if Settings.DEBUG_LEVEL < 2 and not Settings.PROFILING_FUNCS:
    #   cmd.append('--strip-debug')

    if Settings.EXPORT_ALL:
      cmd += ['--no-gc-sections', '--export-all']
    else:
      for export in expand_response(Settings.EXPORTED_FUNCTIONS):
        cmd += ['--export', export[1:]] # Strip the leading underscore

    logging.debug('emcc: lld-linking: %s to %s', files, target)
    t = time.time()
    check_call(cmd)
    if DEBUG:
      logging.debug('  emscript: lld took %s seconds' % (time.time() - t))
      t = time.time()

    return target

  @staticmethod
  def link(files, target, force_archive_contents=False, temp_files=None, just_calculate=False):
    if not temp_files:
      temp_files = configuration.get_temp_files()
    actual_files = []
    # Tracking unresolveds is necessary for .a linking, see below.
    # Specify all possible entry points to seed the linking process.
    # For a simple application, this would just be "main".
    unresolved_symbols = set([func[1:] for func in Settings.EXPORTED_FUNCTIONS])
    resolved_symbols = set()
    # Paths of already included object files from archives.
    added_contents = set()
    has_ar = False
    for f in files:
      if not f.startswith('-'):
        has_ar = has_ar or Building.is_ar(Building.make_paths_absolute(f))

    # If we have only one archive or the force_archive_contents flag is set,
    # then we will add every object file we see, regardless of whether it
    # resolves any undefined symbols.
    force_add_all = len(files) == 1 or force_archive_contents

    # Considers an object file for inclusion in the link. The object is included
    # if force_add=True or if the object provides a currently undefined symbol.
    # If the object is included, the symbol tables are updated and the function
    # returns True.
    def consider_object(f, force_add=False):
      new_symbols = Building.llvm_nm(f)
      # Check if the object was valid according to llvm-nm. It also accepts
      # native object files.
      if not new_symbols.is_valid_for_nm():
        logging.warning('object %s is not valid according to llvm-nm, cannot link' % (f))
        return False
      # Check the object is valid for us, and not a native object file.
      # TODO: for lld, also check if a wasm object file?
      if not Building.is_bitcode(f):
        logging.warning('object %s is not LLVM bitcode, cannot link' % (f))
        return False
      provided = new_symbols.defs.union(new_symbols.commons)
      do_add = force_add or not unresolved_symbols.isdisjoint(provided)
      if do_add:
        logging.debug('adding object %s to link' % (f))
        # Update resolved_symbols table with newly resolved symbols
        resolved_symbols.update(provided)
        # Update unresolved_symbols table by adding newly unresolved symbols and
        # removing newly resolved symbols.
        unresolved_symbols.update(new_symbols.undefs.difference(resolved_symbols))
        unresolved_symbols.difference_update(provided)
        actual_files.append(f)
      return do_add

    # Traverse a single archive. The object files are repeatedly scanned for
    # newly satisfied symbols until no new symbols are found. Returns true if
    # any object files were added to the link.
    def consider_archive(f):
      added_any_objects = False
      loop_again = True
      logging.debug('considering archive %s' % (f))
      contents = Building.ar_contents[f]
      while loop_again: # repeatedly traverse until we have everything we need
        loop_again = False
        for content in contents:
          if content in added_contents:
            continue
          # Link in the .o if it provides symbols, *or* this is a singleton archive (which is apparently an exception in gcc ld)
          if consider_object(content, force_add=force_add_all):
            added_contents.add(content)
            loop_again = True
            added_any_objects = True
      logging.debug('done running loop of archive %s' % (f))
      return added_any_objects

    Building.read_link_inputs([x for x in files if not x.startswith('-')])

    current_archive_group = None

    # Rescan a group of archives until we don't find any more objects to link.
    def scan_archive_group(group):
      loop_again = True
      logging.debug('starting archive group loop')
      while loop_again:
        loop_again = False
        for archive in group:
          if consider_archive(archive):
            loop_again = True
      logging.debug('done with archive group loop')

    for f in files:
      absolute_path_f = Building.make_paths_absolute(f)
      if f.startswith('-'):
        if f in ['--start-group', '-(']:
          assert current_archive_group is None, 'Nested --start-group, missing --end-group?'
          current_archive_group = []
        elif f in ['--end-group', '-)']:
          assert current_archive_group is not None, '--end-group without --start-group'
          scan_archive_group(current_archive_group)
          current_archive_group = None
        else:
          # Command line flags should already be vetted by the time this method
          # is called, so this is an internal error
          assert False, 'unsupported link flag: ' + f
      elif not Building.is_ar(absolute_path_f):
        if Building.is_bitcode(absolute_path_f):
          if has_ar:
            consider_object(absolute_path_f, force_add=True)
          else:
            # If there are no archives then we can simply link all valid bitcode
            # files and skip the symbol table stuff.
            actual_files.append(f)
      else:
        # Extract object files from ar archives, and link according to gnu ld semantics
        # (link in an entire .o from the archive if it supplies symbols still unresolved)
        consider_archive(absolute_path_f)
        # If we're inside a --start-group/--end-group section, add to the list
        # so we can loop back around later.
        if current_archive_group is not None:
          current_archive_group.append(absolute_path_f)

    # We have to consider the possibility that --start-group was used without a matching
    # --end-group; GNU ld permits this behavior and implicitly treats the end of the
    # command line as having an --end-group.
    if current_archive_group:
      logging.debug('--start-group without matching --end-group, rescanning')
      scan_archive_group(current_archive_group)
      current_archive_group = None

    try_delete(target)

    # Finish link
    actual_files = unique_ordered(actual_files) # tolerate people trying to link a.so a.so etc.

    # check for too-long command line
    link_args = actual_files
    # 8k is a bit of an arbitrary limit, but a reasonable one
    # for max command line size before we use a response file
    response_file = None
    if len(' '.join(link_args)) > 8192:
      logging.debug('using response file for llvm-link')
      response_file = temp_files.get(suffix='.response').name

      link_args = ["@" + response_file]

      response_fh = open(response_file, 'w')
      for arg in actual_files:
        # Starting from LLVM 3.9.0 trunk around July 2016, LLVM escapes backslashes in response files, so Windows paths
        # "c:\path\to\file.txt" with single slashes no longer work. LLVM upstream dev 3.9.0 from January 2016 still treated
        # backslashes without escaping. To preserve compatibility with both versions of llvm-link, don't pass backslash
        # path delimiters at all to response files, but always use forward slashes.
        if WINDOWS:
          arg = arg.replace('\\', '/')

        # escaped double quotes allows 'space' characters in pathname the response file can use
        response_fh.write("\"" + arg + "\"\n")
      response_fh.close()

    if not just_calculate:
      logging.debug('emcc: llvm-linking: %s to %s', actual_files, target)
      output = run_process([LLVM_LINK] + link_args + ['-o', target], stdout=PIPE).stdout
      assert os.path.exists(target) and (output is None or 'Could not open input file' not in output), 'Linking error: ' + output
      return target
    else:
      # just calculating; return the link arguments which is the final list of files to link
      return link_args

  # LLVM optimizations
  # @param opt A list of LLVM optimization parameters
  @staticmethod
  def llvm_opt(filename, opts, out=None):
    inputs = filename
    if not isinstance(inputs, list):
      inputs = [inputs]
    else:
      assert out, 'must provide out if llvm_opt on a list of inputs'
    assert len(opts), 'should not call opt with nothing to do'
    opts = opts[:]
    # TODO: disable inlining when needed
    # if not Building.can_inline():
    #   opts.append('-disable-inlining')
    # opts += ['-debug-pass=Arguments']
    if not Settings.SIMD:
      opts += ['-disable-loop-vectorization', '-disable-slp-vectorization', '-vectorize-loops=false', '-vectorize-slp=false']
    else:
      opts += ['-force-vector-width=4']

    logging.debug('emcc: LLVM opts: ' + ' '.join(opts) + '  [num inputs: ' + str(len(inputs)) + ']')
    target = out or (filename + '.opt.bc')
    try:
      run_process([LLVM_OPT] + inputs + opts + ['-o', target], stdout=PIPE)
      assert os.path.exists(target), 'llvm optimizer emitted no output.'
    except subprocess.CalledProcessError as e:
      for i in inputs:
        if not os.path.exists(i):
          logging.warning('Note: Input file "' + i + '" did not exist.')
        elif not Building.is_bitcode(i):
          logging.warning('Note: Input file "' + i + '" exists but was not an LLVM bitcode file suitable for Emscripten. Perhaps accidentally mixing native built object files with Emscripten?')
      exit_with_error('Failed to run llvm optimizations: ' + e.output)
    if not out:
      shutil.move(filename + '.opt.bc', filename)
    return target

  @staticmethod
  def llvm_opts(filename): # deprecated version, only for test runner. TODO: remove
    if Building.LLVM_OPTS:
      shutil.move(filename + '.o', filename + '.o.pre')
      output = run_process([LLVM_OPT, filename + '.o.pre'] + Building.LLVM_OPT_OPTS + ['-o', filename + '.o'], stdout=PIPE).stdout
      assert os.path.exists(filename + '.o'), 'Failed to run llvm optimizations: ' + output

  @staticmethod
  def llvm_dis(input_filename, output_filename=None):
    # LLVM binary ==> LLVM assembly
    if output_filename is None:
      # use test runner conventions
      output_filename = input_filename + '.o.ll'
      input_filename = input_filename + '.o'
    try_delete(output_filename)
    output = run_process([LLVM_DIS, input_filename, '-o', output_filename], stdout=PIPE).stdout
    assert os.path.exists(output_filename), 'Could not create .ll file: ' + output
    return output_filename

  @staticmethod
  def llvm_as(input_filename, output_filename=None):
    # LLVM assembly ==> LLVM binary
    if output_filename is None:
      # use test runner conventions
      output_filename = input_filename + '.o'
      input_filename = input_filename + '.o.ll'
    try_delete(output_filename)
    output = run_process([LLVM_AS, input_filename, '-o', output_filename], stdout=PIPE).stdout
    assert os.path.exists(output_filename), 'Could not create bc file: ' + output
    return output_filename

  @staticmethod
  def parse_symbols(output, include_internal=False):
    defs = []
    undefs = []
    commons = []
    for line in output.split('\n'):
      if len(line) == 0:
        continue
      if ':' in line:
        continue # e.g.  filename.o:  , saying which file it's from
      parts = [seg for seg in line.split(' ') if len(seg)]
      # pnacl-nm will print zero offsets for bitcode, and newer llvm-nm will print present symbols as  -------- T name
      if len(parts) == 3 and parts[0] in ["00000000", "--------"]:
        parts.pop(0)
      if len(parts) == 2:  # ignore lines with absolute offsets, these are not bitcode anyhow (e.g. |00000630 t d_source_name|)
        status, symbol = parts
        if status == 'U':
          undefs.append(symbol)
        elif status == 'C':
          commons.append(symbol)
        elif (not include_internal and status == status.upper()) or \
             (include_internal and status in ['W', 't', 'T', 'd', 'D']): # FIXME: using WTD in the previous line fails due to llvm-nm behavior on macOS,
          #        so for now we assume all uppercase are normally defined external symbols
          defs.append(symbol)
    return ObjectFileInfo(0, None, set(defs), set(undefs), set(commons))

  internal_nm_cache = {} # cache results of nm - it can be slow to run
  uninternal_nm_cache = {}
  ar_contents = {} # Stores the object files contained in different archive files passed as input

  @staticmethod
  def llvm_nm_uncached(filename, stdout=PIPE, stderr=PIPE, include_internal=False):
    # LLVM binary ==> list of symbols
    proc = run_process([LLVM_NM, filename], stdout=stdout, stderr=stderr, check=False)
    if proc.returncode == 0:
      return Building.parse_symbols(proc.stdout, include_internal)
    else:
      return ObjectFileInfo(proc.returncode, str(proc.stdout) + str(proc.stderr))

  @staticmethod
  def llvm_nm(filename, stdout=PIPE, stderr=PIPE, include_internal=False):
    # Always use absolute paths to maximize cache usage
    filename = os.path.abspath(filename)

    if include_internal and filename in Building.internal_nm_cache:
      return Building.internal_nm_cache[filename]
    elif not include_internal and filename in Building.uninternal_nm_cache:
      return Building.uninternal_nm_cache[filename]

    ret = Building.llvm_nm_uncached(filename, stdout, stderr, include_internal)

    if ret.returncode != 0:
      logging.debug('llvm-nm failed on file ' + filename + ': return code ' + str(ret.returncode) + ', error: ' + ret.output)

    # Even if we fail, write the results to the NM cache so that we don't keep trying to llvm-nm the failing file again later.
    if include_internal:
      Building.internal_nm_cache[filename] = ret
    else:
      Building.uninternal_nm_cache[filename] = ret

    return ret

  @staticmethod
  def emcc(filename, args=[], output_filename=None, stdout=None, stderr=None, env=None):
    if output_filename is None:
      output_filename = filename + '.o'
    try_delete(output_filename)
    run_process([PYTHON, EMCC, filename] + args + ['-o', output_filename], stdout=stdout, stderr=stderr, env=env)

  @staticmethod
  def emar(action, output_filename, filenames, stdout=None, stderr=None, env=None):
    try_delete(output_filename)
    run_process([PYTHON, EMAR, action, output_filename] + filenames, stdout=stdout, stderr=stderr, env=env)
    if 'c' in action:
      assert os.path.exists(output_filename), 'emar could not create output file: ' + output_filename

  @staticmethod
  def emscripten(filename, append_ext=True, extra_args=[]):
    if path_from_root() not in sys.path:
      sys.path += [path_from_root()]
    import emscripten
    # Run Emscripten
    cmdline = [filename + ('.o.ll' if append_ext else ''), '-o', filename + '.o.js'] + extra_args
    if jsrun.TRACK_PROCESS_SPAWNS:
      logging.info('Executing emscripten.py compiler with cmdline "' + ' '.join(cmdline) + '"')
    with ToolchainProfiler.profile_block('emscripten.py'):
      emscripten._main(cmdline)

    # Detect compilation crashes and errors
    assert os.path.exists(filename + '.o.js'), 'Emscripten failed to generate .js'

    return filename + '.o.js'

  # TODO: deprecate this method, we should just need Settings.LINKABLE anyhow
  @staticmethod
  def can_build_standalone():
    return not Settings.BUILD_AS_SHARED_LIB and not Settings.LINKABLE

  @staticmethod
  def can_inline():
    return Settings.INLINING_LIMIT == 0

  @staticmethod
  def is_wasm_only():
    # not even wasm, much less wasm-only
    if not Settings.WASM:
      return False
    # llvm backend can only ever produce wasm
    if Settings.WASM_BACKEND:
      return True
    # fastcomp can emit wasm-only code.
    # also disable this mode if it depends on special optimizations that are not yet
    # compatible with it.
    return ('asmjs' not in Settings.BINARYEN_METHOD and 'interpret-asm2wasm' not in Settings.BINARYEN_METHOD and not Settings.RUNNING_JS_OPTS and not Settings.EMULATED_FUNCTION_POINTERS and not Settings.EMULATE_FUNCTION_POINTER_CASTS) or not Settings.LEGALIZE_JS_FFI

  @staticmethod
  def get_safe_internalize():
    if not Building.can_build_standalone():
      return [] # do not internalize anything

    exps = expand_response(Settings.EXPORTED_FUNCTIONS)
    internalize_public_api = '-internalize-public-api-'
    internalize_list = ','.join([exp[1:] for exp in exps])

    # EXPORTED_FUNCTIONS can potentially be very large.
    # 8k is a bit of an arbitrary limit, but a reasonable one
    # for max command line size before we use a response file
    if len(internalize_list) > 8192:
      logging.debug('using response file for EXPORTED_FUNCTIONS in internalize')
      finalized_exports = '\n'.join([exp[1:] for exp in exps])
      internalize_list_file = configuration.get_temp_files().get(suffix='.response').name
      internalize_list_fh = open(internalize_list_file, 'w')
      internalize_list_fh.write(finalized_exports)
      internalize_list_fh.close()
      internalize_public_api += 'file=' + internalize_list_file
    else:
      internalize_public_api += 'list=' + internalize_list

    # internalize carefully, llvm 3.2 will remove even main if not told not to
    return ['-internalize', internalize_public_api]

  @staticmethod
  def opt_level_to_str(opt_level, shrink_level=0):
    # convert opt_level/shrink_level pair to a string argument like -O1
    if opt_level == 0:
      return '-O0'
    if shrink_level == 1:
      return '-Os'
    elif shrink_level >= 2:
      return '-Oz'
    else:
      return '-O' + str(min(opt_level, 3))

  @staticmethod
  def js_optimizer(filename, passes, debug=False, extra_info=None, output_filename=None, just_split=False, just_concat=False):
    from . import js_optimizer
    ret = js_optimizer.run(filename, passes, NODE_JS, debug, extra_info, just_split, just_concat)
    if output_filename:
      safe_move(ret, output_filename)
      ret = output_filename
    return ret

  # run JS optimizer on some JS, ignoring asm.js contents if any - just run on it all
  @staticmethod
  def js_optimizer_no_asmjs(filename, passes, return_output=False, extra_info=None):
    from . import js_optimizer
    original_filename = filename
    if extra_info is not None:
      temp_files = configuration.get_temp_files()
      temp = temp_files.get('.js').name
      shutil.copyfile(filename, temp)
      with open(temp, 'a') as f:
        f.write('// EXTRA_INFO: ' + extra_info)
      filename = temp
    if not return_output:
      next = original_filename + '.jso.js'
      configuration.get_temp_files().note(next)
      check_call(NODE_JS + [js_optimizer.JS_OPTIMIZER, filename] + passes, stdout=open(next, 'w'))
      return next
    else:
      return run_process(NODE_JS + [js_optimizer.JS_OPTIMIZER, filename] + passes, stdout=PIPE).stdout

  # evals ctors. if binaryen_bin is provided, it is the dir of the binaryen tool for this, and we are in wasm mode
  @staticmethod
  def eval_ctors(js_file, binary_file, binaryen_bin='', debug_info=False):
    check_call([PYTHON, path_from_root('tools', 'ctor_evaller.py'), js_file, binary_file, str(Settings.TOTAL_MEMORY), str(Settings.TOTAL_STACK), str(Settings.GLOBAL_BASE), binaryen_bin, str(int(debug_info))])

  @staticmethod
  def eliminate_duplicate_funcs(filename):
    from . import duplicate_function_eliminator
    duplicate_function_eliminator.eliminate_duplicate_funcs(filename)

  @staticmethod
  def calculate_reachable_functions(infile, initial_list, can_reach=True):
    with ToolchainProfiler.profile_block('calculate_reachable_functions'):
      from . import asm_module
      temp = configuration.get_temp_files().get('.js').name
      Building.js_optimizer(infile, ['dumpCallGraph'], output_filename=temp, just_concat=True)
      asm = asm_module.AsmModule(temp)
      lines = asm.funcs_js.split('\n')
      can_call = {}
      for i in range(len(lines)):
        line = lines[i]
        if line.startswith('// REACHABLE '):
          curr = json.loads(line[len('// REACHABLE '):])
          func = curr[0]
          targets = curr[2]
          can_call[func] = set(targets)
      # function tables too - treat a function all as a function that can call anything in it, which is effectively what it is
      for name, funcs in asm.tables.items():
        can_call[name] = set([x.strip() for x in funcs[1:-1].split(',')])
      # print can_call
      # Note: We ignore calls in from outside the asm module, so you could do emterpreted => outside => emterpreted, and we would
      #       miss the first one there. But this is acceptable to do, because we can't save such a stack anyhow, due to the outside!
      # print 'can call', can_call, '\n!!!\n', asm.tables, '!'
      reachable_from = {}
      for func, targets in can_call.items():
        for target in targets:
          if target not in reachable_from:
            reachable_from[target] = set()
          reachable_from[target].add(func)
      # print 'reachable from', reachable_from
      to_check = initial_list[:]
      advised = set()
      if can_reach:
        # find all functions that can reach the initial list
        while len(to_check):
          curr = to_check.pop()
          if curr in reachable_from:
            for reacher in reachable_from[curr]:
              if reacher not in advised:
                if not JS.is_dyn_call(reacher) and not JS.is_function_table(reacher):
                  advised.add(str(reacher))
                to_check.append(reacher)
      else:
        # find all functions that are reachable from the initial list, including it
        # all tables are assumed reachable, as they can be called from dyncall from outside
        for name, funcs in asm.tables.items():
          to_check.append(name)
        while len(to_check):
          curr = to_check.pop()
          if not JS.is_function_table(curr):
            advised.add(curr)
          if curr in can_call:
            for target in can_call[curr]:
              if target not in advised:
                advised.add(str(target))
                to_check.append(target)
      return {'reachable': list(advised), 'total_funcs': len(can_call)}

  @staticmethod
  def closure_compiler(filename, pretty=True):
    with ToolchainProfiler.profile_block('closure_compiler'):
      if not check_closure_compiler():
        logging.error('Cannot run closure compiler')
        raise Exception('closure compiler check failed')

      CLOSURE_EXTERNS = path_from_root('src', 'closure-externs.js')
      NODE_EXTERNS_BASE = path_from_root('third_party', 'closure-compiler', 'node-externs')
      NODE_EXTERNS = os.listdir(NODE_EXTERNS_BASE)
      NODE_EXTERNS = [os.path.join(NODE_EXTERNS_BASE, name) for name in NODE_EXTERNS
                      if name.endswith('.js')]
      BROWSER_EXTERNS_BASE = path_from_root('third_party', 'closure-compiler', 'browser-externs')
      BROWSER_EXTERNS = os.listdir(BROWSER_EXTERNS_BASE)
      BROWSER_EXTERNS = [os.path.join(BROWSER_EXTERNS_BASE, name) for name in BROWSER_EXTERNS
                         if name.endswith('.js')]

      # Something like this (adjust memory as needed):
      #   java -Xmx1024m -jar CLOSURE_COMPILER --compilation_level ADVANCED_OPTIMIZATIONS --variable_map_output_file src.cpp.o.js.vars --js src.cpp.o.js --js_output_file src.cpp.o.cc.js
      args = [JAVA,
              '-Xmx' + (os.environ.get('JAVA_HEAP_SIZE') or '1024m'), # if you need a larger Java heap, use this environment variable
              '-jar', CLOSURE_COMPILER,
              '--compilation_level', 'ADVANCED_OPTIMIZATIONS',
              '--language_in', 'ECMASCRIPT5',
              '--externs', CLOSURE_EXTERNS,
              # '--variable_map_output_file', filename + '.vars',
              '--js', filename, '--js_output_file', filename + '.cc.js']
      for extern in NODE_EXTERNS:
        args.append('--externs')
        args.append(extern)
      for extern in BROWSER_EXTERNS:
        args.append('--externs')
        args.append(extern)
      if Settings.IGNORE_CLOSURE_COMPILER_ERRORS:
        args.append('--jscomp_off=*')
      if pretty:
        args += ['--formatting', 'PRETTY_PRINT']
      if os.environ.get('EMCC_CLOSURE_ARGS'):
        args += shlex.split(os.environ.get('EMCC_CLOSURE_ARGS'))
      logging.debug('closure compiler: ' + ' '.join(args))
      process = run_process(args, stdout=PIPE, stderr=STDOUT, check=False)
      if process.returncode != 0 or not os.path.exists(filename + '.cc.js'):
        raise Exception('closure compiler error: ' + process.stdout + ' (rc: %d)' % process.returncode)

      return filename + '.cc.js'

  # minify the final wasm+JS combination. this is done after all the JS
  # and wasm optimizations; here we do the very final optimizations on them
  @staticmethod
  def minify_wasm_js(js_file, wasm_file, expensive_optimizations, minify_whitespace, use_closure_compiler, debug_info, emit_symbol_map):
    # start with JSDCE, to clean up obvious JS garbage. When optimizing for size,
    # use AJSDCE (aggressive JS DCE, performs multiple iterations). Clean up
    # whitespace if necessary too.
    passes = []
    if not Settings.LINKABLE:
      passes.append('JSDCE' if not expensive_optimizations else 'AJSDCE')
    if minify_whitespace:
      passes.append('minifyWhitespace')
    if passes:
      logging.debug('running cleanup on shell code: ' + ' '.join(passes))
      js_file = Building.js_optimizer_no_asmjs(js_file, ['noPrintMetadata'] + passes)
    # if we can optimize this js+wasm combination under the assumption no one else
    # will see the internals, do so
    if not Settings.LINKABLE:
      # if we are optimizing for size, shrink the combined wasm+JS
      # TODO: support this when a symbol map is used
      if expensive_optimizations and not emit_symbol_map:
        js_file = Building.metadce(js_file, wasm_file, minify_whitespace=minify_whitespace, debug_info=debug_info)
        # now that we removed unneeded communication between js and wasm, we can clean up
        # the js some more.
        passes = ['noPrintMetadata', 'AJSDCE']
        if minify_whitespace:
          passes.append('minifyWhitespace')
        logging.debug('running post-meta-DCE cleanup on shell code: ' + ' '.join(passes))
        js_file = Building.js_optimizer_no_asmjs(js_file, passes)
      # finally, optionally use closure compiler to finish cleaning up the JS
      if use_closure_compiler:
        logging.debug('running closure on shell code')
        js_file = Building.closure_compiler(js_file, pretty=not minify_whitespace)
    return js_file

  # run binaryen's wasm-metadce to dce both js and wasm
  @staticmethod
  def metadce(js_file, wasm_file, minify_whitespace, debug_info):
    logging.debug('running meta-DCE')
    temp_files = configuration.get_temp_files()
    # first, get the JS part of the graph
    txt = Building.js_optimizer_no_asmjs(js_file, ['emitDCEGraph', 'noEmitAst'], return_output=True)
    graph = json.loads(txt)
    # ensure that functions expected to be exported to the outside are roots
    for item in graph:
      if 'export' in item:
        export = item['export']
        # wasm backend's exports are prefixed differently inside the wasm
        if Settings.WASM_BACKEND:
          export = '_' + export
        if export in Building.user_requested_exports or Settings.EXPORT_ALL:
          item['root'] = True
    if Settings.WASM_BACKEND:
      # wasm backend's imports are prefixed differently inside the wasm
      for item in graph:
        if 'import' in item:
          if item['import'][1][0] == '_':
            item['import'][1] = item['import'][1][1:]
    temp = temp_files.get('.txt').name
    txt = json.dumps(graph)
    with open(temp, 'w') as f:
      f.write(txt)
    # run wasm-metadce
    cmd = [os.path.join(Building.get_binaryen_bin(), 'wasm-metadce'), '--graph-file=' + temp, wasm_file, '-o', wasm_file]
    if debug_info:
      cmd += ['-g']
    out = run_process(cmd, stdout=PIPE).stdout
    # find the unused things in js
    unused = []
    PREFIX = 'unused: '
    for line in out.split('\n'):
      if line.startswith(PREFIX):
        name = line.replace(PREFIX, '').strip()
        unused.append(name)
    # remove them
    passes = ['applyDCEGraphRemovals']
    if minify_whitespace:
      passes.append('minifyWhitespace')
    extra_info = {'unused': unused}
    return Building.js_optimizer_no_asmjs(js_file, passes, extra_info=json.dumps(extra_info))

  # the exports the user requested
  user_requested_exports = []
  _is_ar_cache = {}

  @staticmethod
  def is_ar(filename):
    try:
      if Building._is_ar_cache.get(filename):
        return Building._is_ar_cache[filename]
      b = bytearray(open(filename, 'rb').read(8))
      sigcheck = b[0] == ord('!') and b[1] == ord('<') and \
                 b[2] == ord('a') and b[3] == ord('r') and \
                 b[4] == ord('c') and b[5] == ord('h') and \
                 b[6] == ord('>') and b[7] == 10
      Building._is_ar_cache[filename] = sigcheck
      return sigcheck
    except Exception as e:
      logging.debug('Building.is_ar failed to test whether file \'%s\' is a llvm archive file! Failed on exception: %s' % (filename, e))
      return False

  @staticmethod
  def is_bitcode(filename):
    # look for magic signature
    b = bytearray(open(filename, 'rb').read(4))
    if len(b) < 4:
      return False
    if b[0] == ord('B') and b[1] == ord('C'):
      return True
    # look for ar signature
    elif Building.is_ar(filename):
      return True
    # on macOS, there is a 20-byte prefix
    elif b[0] == 222 and b[1] == 192 and b[2] == 23 and b[3] == 11:
      b = bytearray(open(filename, 'rb').read(24))
      return b[20] == ord('B') and b[21] == ord('C')

    return False

  @staticmethod
  # Given the name of a special Emscripten-implemented system library, returns an array of absolute paths to JS library
  # files inside emscripten/src/ that corresponds to the library name.
  def path_to_system_js_libraries(library_name):
    # Some native libraries are implemented in Emscripten as system side JS libraries
    js_system_libraries = {
      'c': '',
      'dl': '',
      'EGL': 'library_egl.js',
      'GL': 'library_gl.js',
      'GLESv2': 'library_gl.js',
      'GLEW': 'library_glew.js',
      'glfw': 'library_glfw.js',
      'glfw3': 'library_glfw.js',
      'GLU': '',
      'glut': 'library_glut.js',
      'm': '',
      'openal': 'library_openal.js',
      'rt': '',
      'pthread': '',
      'X11': 'library_xlib.js',
      'SDL': 'library_sdl.js',
      'stdc++': '',
      'uuid': 'library_uuid.js'
    }
    library_files = []
    if library_name in js_system_libraries:
      if len(js_system_libraries[library_name]):
        library_files += [js_system_libraries[library_name]]

        # TODO: This is unintentional due to historical reasons. Improve EGL to use HTML5 API to avoid depending on GLUT.
        if library_name == 'EGL':
          library_files += ['library_glut.js']

    elif library_name.endswith('.js') and os.path.isfile(path_from_root('src', 'library_' + library_name)):
      library_files += ['library_' + library_name]
    else:
      if Settings.ERROR_ON_MISSING_LIBRARIES:
        exit_with_error('emcc: cannot find library "%s"', library_name)
      else:
        logging.warning('emcc: cannot find library "%s"', library_name)

    return library_files

  @staticmethod
  # Given a list of Emscripten link settings, returns a list of paths to system JS libraries
  # that should get linked automatically in to the build when those link settings are present.
  def path_to_system_js_libraries_for_settings(link_settings):
    system_js_libraries = []
    if 'EMTERPRETIFY_ASYNC=1' in link_settings:
      system_js_libraries += ['library_async.js']
    if 'ASYNCIFY=1' in link_settings:
      system_js_libraries += ['library_async.js']
    if 'LZ4=1' in link_settings:
      system_js_libraries += ['library_lz4.js']
    if 'USE_SDL=1' in link_settings:
      system_js_libraries += ['library_sdl.js']
    if 'USE_SDL=2' in link_settings:
      system_js_libraries += ['library_egl.js', 'library_glut.js', 'library_gl.js']
    return [path_from_root('src', x) for x in system_js_libraries]

  @staticmethod
  def get_binaryen():
    # fetch the port, so we have binaryen set up. indicate we need binaryen
    # using the settings
    from . import system_libs
    old = Settings.WASM
    Settings.WASM = 1
    system_libs.get_port('binaryen', Settings)
    Settings.WASM = old

  @staticmethod
  def get_binaryen_bin():
    Building.get_binaryen()
    return os.path.join(Settings.BINARYEN_ROOT, 'bin')

  @staticmethod
  def get_binaryen_lib():
    Building.get_binaryen()
    # The wasm.js and binaryen.js libraries live in 'bin' in the binaryen
    # source tree, but are installed to share/binaryen.
    paths = (os.path.join(Settings.BINARYEN_ROOT, 'bin'),
             os.path.join(Settings.BINARYEN_ROOT, 'share', 'binaryen'))
    for dirname in paths:
      if os.path.exists(os.path.join(dirname, 'wasm.js')):
        return dirname
    exit_with_error('emcc: cannot find binaryen js libraries (tried: %s)', str(paths))


# compatibility with existing emcc, etc. scripts
Cache = cache.Cache(debug=DEBUG_CACHE)
chunkify = cache.chunkify


def reconfigure_cache():
  global Cache
  Cache = cache.Cache(debug=DEBUG_CACHE)


# Placeholder strings used for SINGLE_FILE
class FilenameReplacementStrings:
  WASM_TEXT_FILE = '{{{ FILENAME_REPLACEMENT_STRINGS_WASM_TEXT_FILE }}}'
  WASM_BINARY_FILE = '{{{ FILENAME_REPLACEMENT_STRINGS_WASM_BINARY_FILE }}}'
  ASMJS_CODE_FILE = '{{{ FILENAME_REPLACEMENT_STRINGS_ASMJS_CODE_FILE }}}'


class JS(object):
  memory_initializer_pattern = '/\* memory initializer \*/ allocate\(\[([\d, ]*)\], "i8", ALLOC_NONE, ([\d+\.GLOBAL_BASEHgb]+)\);'
  no_memory_initializer_pattern = '/\* no memory initializer \*/'

  memory_staticbump_pattern = 'STATICTOP = STATIC_BASE \+ (\d+);'

  global_initializers_pattern = '/\* global initializers \*/ __ATINIT__.push\((.+)\);'

  module_export_name_substitution_pattern = '"__EMSCRIPTEN_PRIVATE_MODULE_EXPORT_NAME_SUBSTITUTION__"'

  @staticmethod
  def to_nice_ident(ident): # limited version of the JS function toNiceIdent
    return ident.replace('%', '$').replace('@', '_').replace('.', '_')

  # Returns the given string with escapes added so that it can safely be placed inside a string in JS code.
  @staticmethod
  def escape_for_js_string(s):
    s = s.replace('\\', '/').replace("'", "\\'").replace('"', '\\"')
    return s

  # Returns the subresource location for run-time access
  @staticmethod
  def get_subresource_location(path, data_uri=None):
    if data_uri is None:
      data_uri = Settings.SINGLE_FILE
    if data_uri:
      f = open(path, 'rb')
      data = base64.b64encode(f.read())
      f.close()
      return 'data:application/octet-stream;base64,' + asstr(data)
    else:
      return os.path.basename(path)

  @staticmethod
  def make_initializer(sig, settings=None):
    settings = settings or Settings
    if sig == 'i':
      return '0'
    elif sig == 'f' and settings.get('PRECISE_F32'):
      return 'Math_fround(0)'
    elif sig == 'j':
      if settings:
        assert settings['WASM'], 'j aka i64 only makes sense in wasm-only mode in binaryen'
      return 'i64(0)'
    elif sig == 'F':
      return 'SIMD_Float32x4_check(SIMD_Float32x4(0,0,0,0))'
    elif sig == 'D':
      return 'SIMD_Float64x2_check(SIMD_Float64x2(0,0,0,0))'
    elif sig == 'B':
      return 'SIMD_Int8x16_check(SIMD_Int8x16(0,0,0,0))'
    elif sig == 'S':
      return 'SIMD_Int16x8_check(SIMD_Int16x8(0,0,0,0))'
    elif sig == 'I':
      return 'SIMD_Int32x4_check(SIMD_Int32x4(0,0,0,0))'
    else:
      return '+0'

  FLOAT_SIGS = ['f', 'd']

  @staticmethod
  def make_coercion(value, sig, settings=None, ffi_arg=False, ffi_result=False, convert_from=None):
    settings = settings or Settings
    if sig == 'i':
      if convert_from in JS.FLOAT_SIGS:
        value = '(~~' + value + ')'
      return value + '|0'
    if sig in JS.FLOAT_SIGS and convert_from == 'i':
      value = '(' + value + '|0)'
    if sig == 'f' and settings.get('PRECISE_F32'):
      if ffi_arg:
        return '+Math_fround(' + value + ')'
      elif ffi_result:
        return 'Math_fround(+(' + value + '))'
      else:
        return 'Math_fround(' + value + ')'
    elif sig == 'd' or sig == 'f':
      return '+' + value
    elif sig == 'j':
      if settings:
        assert settings['WASM'], 'j aka i64 only makes sense in wasm-only mode in binaryen'
      return 'i64(' + value + ')'
    elif sig == 'F':
      return 'SIMD_Float32x4_check(' + value + ')'
    elif sig == 'D':
      return 'SIMD_Float64x2_check(' + value + ')'
    elif sig == 'B':
      return 'SIMD_Int8x16_check(' + value + ')'
    elif sig == 'S':
      return 'SIMD_Int16x8_check(' + value + ')'
    elif sig == 'I':
      return 'SIMD_Int32x4_check(' + value + ')'
    else:
      return value

  @staticmethod
  def legalize_sig(sig):
    ret = [sig[0]]
    for s in sig[1:]:
      if s != 'j':
        ret.append(s)
      else:
        # an i64 is legalized into i32, i32
        ret.append('i')
        ret.append('i')
    return ''.join(ret)

  @staticmethod
  def make_extcall(sig, named=True):
    args = ','.join(['a' + str(i) for i in range(1, len(sig))])
    args = 'index' + (',' if args else '') + args
    # C++ exceptions are numbers, and longjmp is a string 'longjmp'
    ret = '''function%s(%s) {
  %sModule["dynCall_%s"](%s);
}''' % ((' extCall_' + sig) if named else '', args, 'return ' if sig[0] != 'v' else '', sig, args)
    return ret

  @staticmethod
  def make_jscall(sig, sig_order=0, named=True):
    fnargs = ','.join(['a' + str(i) for i in range(1, len(sig))])
    args = 'index' + (',' if fnargs else '') + fnargs
    # While asm.js/fastcomp's addFunction support preallocates
    # Settings.RESERVED_FUNCTION_POINTERS slots in functionPointers array, on
    # the Wasm backend we reserve that number of slots for each possible
    # function signature, so it is (Settings.RESERVED_FUNCTION_POINTERS * # of
    # indirectly called function signatures) slots in total. So the index to
    # functionPointers array should be adjusted according to the order of the
    # function signature. The reason we do this is Wasm has a single unified
    # function table while asm.js maintains separate function table per
    # signature.
    # e.g. When there are three possible function signature, ['v', 'ii', 'ff'],
    # the 'sig_order' parameter will be 0 for 'v', 1 for 'ii', and so on.
    if Settings.WASM_BACKEND:
      index = 'index + %d' % (Settings.RESERVED_FUNCTION_POINTERS * sig_order)
    else:
      index = 'index'
    ret = '''function%s(%s) {
    %sfunctionPointers[%s](%s);
}''' % ((' jsCall_' + sig) if named else '', args, 'return ' if sig[0] != 'v' else '', index, fnargs)
    return ret

  @staticmethod
  def make_invoke(sig, named=True):
    legal_sig = JS.legalize_sig(sig) # TODO: do this in extcall, jscall?
    args = ','.join(['a' + str(i) for i in range(1, len(legal_sig))])
    args = 'index' + (',' if args else '') + args
    # C++ exceptions are numbers, and longjmp is a string 'longjmp'
    ret = '''function%s(%s) {
  var sp = stackSave();
  try {
    %sModule["dynCall_%s"](%s);
  } catch(e) {
    stackRestore(sp);
    if (typeof e !== 'number' && e !== 'longjmp') throw e;
    Module["setThrew"](1, 0);
  }
}''' % ((' invoke_' + sig) if named else '', args, 'return ' if sig[0] != 'v' else '', sig, args)
    return ret

  @staticmethod
  def align(x, by):
    while x % by != 0:
      x += 1
    return x

  @staticmethod
  def generate_string_initializer(s):
    if Settings.ASSERTIONS:
      # append checksum of length and content
      crcTable = []
      for i in range(256):
        crc = i
        for bit in range(8):
          crc = (crc >> 1) ^ ((crc & 1) * 0xedb88320)
        crcTable.append(crc)
      crc = 0xffffffff
      n = len(s)
      crc = crcTable[(crc ^ n) & 0xff] ^ (crc >> 8)
      crc = crcTable[(crc ^ (n >> 8)) & 0xff] ^ (crc >> 8)
      for i in s:
        crc = crcTable[(crc ^ i) & 0xff] ^ (crc >> 8)
      for i in range(4):
        s.append((crc >> (8 * i)) & 0xff)
    s = ''.join(map(chr, s))
    s = s.replace('\\', '\\\\').replace("'", "\\'")
    s = s.replace('\n', '\\n').replace('\r', '\\r')

    # Escape the ^Z (= 0x1a = substitute) ASCII character and all characters higher than 7-bit ASCII.
    def escape(x):
      return '\\x{:02x}'.format(ord(x.group()))

    return re.sub('[\x1a\x80-\xff]', escape, s)

  @staticmethod
  def is_dyn_call(func):
    return func.startswith('dynCall_')

  @staticmethod
  def is_function_table(name):
    return name.startswith('FUNCTION_TABLE_')


class WebAssembly(object):
  @staticmethod
  def lebify(x):
    assert x >= 0, 'TODO: signed'
    ret = []
    while 1:
      byte = x & 127
      x >>= 7
      more = x != 0
      if more:
        byte = byte | 128
      ret.append(byte)
      if not more:
        break
    return bytearray(ret)

  @staticmethod
  def make_shared_library(js_file, wasm_file):
    # a wasm shared library has a special "dylink" section, see tools-conventions repo
    js = open(js_file).read()
    m = re.search("var STATIC_BUMP = (\d+);", js)
    mem_size = int(m.group(1))
    m = re.search("Module\['wasmTableSize'\] = (\d+);", js)
    table_size = int(m.group(1))
    m = re.search('gb = alignMemory\(getMemory\(\d+ \+ (\d+)\), (\d+) \|\| 1\);', js)
    assert m.group(1) == m.group(2), 'js must contain a clear alignment for the wasm shared library'
    mem_align = int(m.group(1))
    mem_align = int(math.log(mem_align, 2))
    logging.debug('creating wasm dynamic library with mem size %d, table size %d, align %d' % (mem_size, table_size, mem_align))
    wso = js_file + '.wso'
    # write the binary
    wasm = open(wasm_file, 'rb').read()
    f = open(wso, 'wb')
    f.write(wasm[0:8]) # copy magic number and version
    # write the special section
    f.write(b'\0') # user section is code 0
    # need to find the size of this section
    name = b"\06dylink" # section name, including prefixed size
    contents = (WebAssembly.lebify(mem_size) + WebAssembly.lebify(mem_align) +
                WebAssembly.lebify(table_size) + WebAssembly.lebify(0))
    size = len(name) + len(contents)
    f.write(WebAssembly.lebify(size))
    f.write(name)
    f.write(contents)
    f.write(wasm[8:]) # copy rest of binary
    f.close()
    return wso


# Python 2-3 compatibility helper function:
# Converts a string to the native str type.
def asstr(s):
  if str is bytes:
    if isinstance(s, unicode):
      return s.encode('utf-8')
  elif isinstance(s, bytes):
    return s.decode('utf-8')
  return s


def asbytes(s):
  if str is bytes:
    # Python 2 compatibility:
    # s.encode implicitly will first call s.decode('ascii') which may fail when with Unicode characters
    return s
  return s.encode('utf-8')


def suffix(name):
  """Return the file extension *not* including the '.'."""
  return os.path.splitext(name)[1][1:]


def unsuffixed(name):
  """Return the filename without the extention.

  If there are multiple extensions this strips only the final one.
  """
  return os.path.splitext(name)[0]


def unsuffixed_basename(name):
  return os.path.basename(unsuffixed(name))


def safe_move(src, dst):
  src = os.path.abspath(src)
  dst = os.path.abspath(dst)
  if os.path.isdir(dst):
    dst = os.path.join(dst, os.path.basename(src))
  if src == dst:
    return
  if dst == '/dev/null':
    return
  shutil.move(src, dst)


def safe_copy(src, dst):
  src = os.path.abspath(src)
  dst = os.path.abspath(dst)
  if os.path.isdir(dst):
    dst = os.path.join(dst, os.path.basename(src))
  if src == dst:
    return
  if dst == '/dev/null':
    return
  shutil.copyfile(src, dst)


def clang_preprocess(filename):
  # TODO: REMOVE HACK AND PASS PREPROCESSOR FLAGS TO CLANG.
  return run_process([CLANG_CC, '-DFETCH_DEBUG=1', '-E', '-P', '-C', '-x', 'c', filename], check=True, stdout=subprocess.PIPE).stdout


def read_and_preprocess(filename):
  temp_dir = get_emscripten_temp_dir()
  # Create a settings file with the current settings to pass to the JS preprocessor
  # Note: Settings.serialize returns an array of -s options i.e. ['-s', '<setting1>', '-s', '<setting2>', ...]
  #       we only want the actual settings, hence the [1::2] slice operation.
  settings_str = "var " + ";\nvar ".join(Settings.serialize()[1::2])
  settings_file = os.path.join(temp_dir, 'settings.js')
  open(settings_file, 'w').write(settings_str)

  # Run the JS preprocessor
  # N.B. We can't use the default stdout=PIPE here as it only allows 64K of output before it hangs
  # and shell.html is bigger than that!
  # See https://thraxil.org/users/anders/posts/2008/03/13/Subprocess-Hanging-PIPE-is-your-enemy/
  (path, file) = os.path.split(filename)
  if not path:
    path = None
  stdout = os.path.join(temp_dir, 'stdout')
  args = [settings_file, file]

  run_js(path_from_root('tools/preprocessor.js'), NODE_JS, args, True, stdout=open(stdout, 'w'), cwd=path)
  out = open(stdout, 'r').read()

  return out


# Generates a suitable fetch-worker.js script from the given input source JS file (which is an asm.js build output),
# and writes it out to location output_file. fetch-worker.js is the root entry point for a dedicated filesystem web
# worker in -s ASMFS=1 mode.
def make_fetch_worker(source_file, output_file):
  src = open(source_file, 'r').read()
  funcs_to_import = ['alignUp', 'getTotalMemory', 'stringToUTF8', 'intArrayFromString', 'lengthBytesUTF8', 'stringToUTF8Array', '_emscripten_is_main_runtime_thread', '_emscripten_futex_wait']
  asm_funcs_to_import = ['_malloc', '_free', '_sbrk', '___pthread_mutex_lock', '___pthread_mutex_unlock']
  function_prologue = '''this.onerror = function(e) {
  console.error(e);
}

'''
  asm_start = src.find('// EMSCRIPTEN_START_ASM')
  for func in funcs_to_import + asm_funcs_to_import:
    loc = src.find('function ' + func + '(', asm_start if func in asm_funcs_to_import else 0)
    if loc == -1:
      exit_with_error('failed to find function %s!', func)
    end_loc = src.find('{', loc) + 1
    nesting_level = 1
    while nesting_level > 0:
      if src[end_loc] == '{':
        nesting_level += 1
      if src[end_loc] == '}':
        nesting_level -= 1
      end_loc += 1

    func_code = src[loc:end_loc]
    function_prologue = function_prologue + '\n' + func_code

  fetch_worker_src = function_prologue + '\n' + clang_preprocess(path_from_root('src', 'fetch-worker.js'))
  open(output_file, 'w').write(fetch_worker_src)
