
"""

Note: running a script or add-in via this add-in will not cause the script/add-in to appear 
in Fusion360's "Scripts and Add-Ins" dialog.  This is one of the ways in which the action of this add-in is not
exactly identical to the action of manual commands issued in Fusion360's user interface.
"""

# The structure and much of the function of this code is inspired by Ben Gruver's fusion_idea_addin

import adsk
import adsk.core
import adsk.fusion
import hashlib
import http.client
# from http.server import HTTPServer, BaseHTTPRequestHandler
import http.server
import importlib
import importlib.util
import io
import json
import logging
import logging.handlers
import os
import re
import socket
import socketserver
import struct
import sys
import threading
import traceback
from typing import Optional, Callable
import urllib.parse
import tempfile

import shutil

import datetime
import pathlib
# sys.path.append(str(pathlib.Path(__file__).parent.parent.joinpath('lib').resolve()))
sys.path.append(str(pathlib.Path(__file__).joinpath('lib').resolve()))
from simple_fusion_custom_command import SimpleFusionCustomCommand
import fusion_main_thread_runner

 
import rpyc
import rpyc.core
import rpyc.utils.server



NAME_OF_THIS_ADDIN = 'fusion_script_runner_addin'
PORT_NUMBER_FOR_RPYC_SLAVE_SERVER = 18812
PORT_NUMBER_FOR_HTTP_SERVER = 19812

debugpy = None
debugging_started = False

pathOfDebuggingLog = os.path.join(tempfile.gettempdir(), f"{NAME_OF_THIS_ADDIN}_log.log")



def app() -> adsk.core.Application: return adsk.core.Application.get()
def ui() -> adsk.core.UserInterface: return app().userInterface

logger = logging.getLogger(NAME_OF_THIS_ADDIN)
logger.propagate = False

class AddIn(object):
    def __init__(self):
        self._logging_file_handler                  : Optional[logging.Handler]                 = None
        self._logging_dialog_handler                : Optional[logging.Handler]                 = None
        self._http_server                           : Optional[http.server.HTTPServer]          = None
        self._rpyc_slave_server                     : Optional[rpyc.utils.server.Server]        = None
        self._fusionMainThreadRunner                : Optional[fusion_main_thread_runner.FusionMainThreadRunner]          = None
        self._simpleFusionCustomCommands            : list[SimpleFusionCustomCommand]           = []

    def start(self):
        
        # logging-related setup, in its own try block because, once logging is properly set up,
        # we will use the logging infrastructure to log error messages in the Except block,
        # but here, before logging infrasturcture is set up, the Except block will report
        # error messages in a more primitive way.
        try:
            
            self._logging_file_handler = logging.handlers.RotatingFileHandler(
                filename=pathOfDebuggingLog,
                maxBytes=2**20,
                backupCount=1)
            self._logging_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            logger.addHandler(self._logging_file_handler)
            # logger.setLevel(logging.WARNING)
            logger.setLevel(logging.DEBUG)

            if False:
                self._logging_dialog_handler = FusionErrorDialogLoggingHandler()
                self._logging_dialog_handler.setFormatter(logging.Formatter("%(message)s"))
                self._logging_dialog_handler.setLevel(logging.FATAL)
                logger.addHandler(self._logging_dialog_handler)

            self._logging_textcommands_palette_handler = FusionTextCommandsPalletteLoggingHandler()
            self._logging_textcommands_palette_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self._logging_textcommands_palette_handler.setLevel(logging.DEBUG)
            logger.addHandler(self._logging_textcommands_palette_handler)

        except Exception:
            # The logging infrastructure may not be set up yet, so we directly show an error dialog instead
            ui().messageBox(f"Error while starting {NAME_OF_THIS_ADDIN}.\n\n%s" % traceback.format_exc())
            return

        try:
            # try:
            #     app().unregisterCustomEvent(RUN_SCRIPT_REQUESTED_EVENT_ID)
            # except Exception:
            #     pass

            logger.debug(f"Hello from {__file__}")
            logger.debug("os.getcwd(): " + os.getcwd())

            
            self._fusionMainThreadRunner = fusion_main_thread_runner.FusionMainThreadRunner(logger=logger)
            # self._run_script_requested_event = app().registerCustomEvent(RUN_SCRIPT_REQUESTED_EVENT_ID)
            # self._run_script_requested_event_handler = RunScriptRequestedEventHandler()
            # self._run_script_requested_event.add(self._run_script_requested_event_handler)

            # Ben Gruver would run the http server on a random port, to avoid conflicts when multiple instances of Fusion 360 are
            # running, and would have the client use SSDP to discover the correct desired port to connect to.
            # I am, at present, simplifying things and simply using a hard-coded port number.
            self._http_server = http.server.HTTPServer(("localhost", PORT_NUMBER_FOR_HTTP_SERVER), RunScriptHTTPRequestHandler)

            http_server_thread = threading.Thread(target=self.run_http_server, daemon=True)
            http_server_thread.start()

            self._rpyc_slave_server = rpyc.ThreadedServer(
                rpyc.SlaveService,
                hostname='localhost',
                port=PORT_NUMBER_FOR_RPYC_SLAVE_SERVER,
                reuse_addr=True,
                ipv6=False, 
                authenticator=None,
                registrar=None, 
                auto_register=False
            )

            rpyc_slave_server_thread = threading.Thread(target=self.run_rpyc_slave_server, daemon=True)
            rpyc_slave_server_thread.start()

            def myTestFunction(eventArgs: adsk.core.CommandEventArgs)  -> None:
                logger.debug("myTestFunction was called.")
                return None

            self._simpleFusionCustomCommands.append(SimpleFusionCustomCommand(name="neil_cool_command1", action=myTestFunction, app=app(), logger=logger))

        except Exception:
            logger.fatal(f"Error while starting {NAME_OF_THIS_ADDIN}", exc_info=sys.exc_info())

    def run_http_server(self):
        logger.debug("starting http server: port=%d" % self._http_server.server_port)
        try:
            with self._http_server:
                self._http_server.serve_forever()
        except Exception:
            logger.fatal("Error occurred while starting the http server.", exc_info=sys.exc_info())

    def run_rpyc_slave_server(self):
        #TO DO: add exception handling
        self._rpyc_slave_server.start()

    #this is intended to be run in Fusion's main thread.
    def runScript(self, 
        script_path  : str, 
        debug        : bool = False, 
        debugpy_path : str  = "", 
        debug_port   : int  = 0,
        prefixes_of_submodules_not_to_be_reloaded : 'list[str]' = []
    ):
        try:
            if not script_path and not debug:
                logger.warning("No script provided and debugging not requested. There's nothing to do.")
                return

            if debug: ensureThatDebuggingIsStarted(debugpy_path=debugpy_path, debug_port=debug_port)
                
            if script_path:
                if debug:
                    if not debugpy.is_client_connected():
                        ui().palettes.itemById('TextCommands').writeText(str(datetime.datetime.now()) + "\t" + 'Waiting for connection from client, and will then run ' + script_path)
                        adsk.doEvents() #this seems to be necessary to make the message appear in the textCommands palette right away.
                    debugpy.wait_for_client()
                    ui().palettes.itemById('TextCommands').writeText(str(datetime.datetime.now()) + "\t" + 'Client connected.  Now running ' + script_path + ' ...')
                    adsk.doEvents() 
                    # we might consider doing this waiting in a separate thread so as to not block the UI.
                    
                script_path = os.path.abspath(script_path)
                script_dir = os.path.dirname(script_path)

                try:
                    # This mostly mimics the package name that Fusion uses when running the script
                    module_name = "__main__" + urllib.parse.quote(script_path.replace('.', '_'))
                    spec = importlib.util.spec_from_file_location(
                        module_name, script_path, submodule_search_locations=[script_dir])
                    module = importlib.util.module_from_spec(spec)

                    existing_module = sys.modules.get(module_name)
                    if existing_module and hasattr(existing_module, "stop"):
                        try:
                            existing_module.stop({"isApplicationClosing": False})
                        except Exception:
                            if debug:
                                logger.warning(
                                    "Unhandled exception while attempting to call the script's 'stop' function.",
                                    exc_info=sys.exc_info()
                                )
                                # if debug is true, we assume that we could be dealing with a buggy script and we also assume that
                                # the user is probably someone working on the script being run rather than simply a user wanting to 
                                # use the script.  Therefore, if the debug flag is true, we will swallow an exception 
                                # caused by invoking the script's stop method and let the show go on (possibly toward an eventual crash).
                                # If the debug flag is not set, we will raise the exception caused by invoking the script's 'stop' function.
                            else:
                                raise

                    unload_submodules(module_name, prefixes_of_submodules_not_to_be_reloaded)

                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                    logger.debug("Running script")
                    module.run({"isApplicationStartup": False})
                except Exception:
                    logger.fatal(
                        "Unhandled exception while importing and running script.",
                        exc_info=sys.exc_info()
                    )
            # i = 0
            # # wait_for_client experiment
            # while i<5:
            #     debugpy.wait_for_client()
            #     # we seem to be iterating on the initial connection (i.e. pressing F5 in VS code)
            #     # and on pressing the restart button in VS code.
            #     # this is good -- this observation is consistent with Fusion using wait_for_client to
            #     # for all interaction with vs code.
            #     # Presumably, when I press F5 in vs code, vs code sends to the debug adapter information
            #     # about which file is active in VS code.  Is this information accessible here?
            #     # Does Fusion's behavior when initiaiting debugging from the UI buttons require 
            #     # Fusion to know which file is active in VS code?  In other words, once we have used
            #     # the Fusion UI buttons to start a script or add-in in debug mode, does Fusion behave any differently in 
            #     # response to a pres of F% in VS code depending on which file is active in VS code.  (My hunch is no.)
            #     # Actually, fusion does seem to know at least the parent directory of the file active in vs code.
            #     # I reckon Fusion must be getting this information from the gloabal PyDB object.

            #     #Based on the messages that I caught with my LoggingDapMessagesListener, it looks like
            #     # the path information the vscode sends to the debug adapter is precisely the information
            #     # defined in the vscode launch.json file -- that makes sense.  But how is it happening that
            #     # fusion runs the add-in/script specified by that path.  Is Fusion doing that, or is Fusion 
            #     # setting up debugpy to do that.  In either case, how?


            #     pydb.block_until_configuration_done

            #     i += 1
            #     logger.debug(f"client connected {i}")
            #     while debugpy.is_client_connected():
            #         pass

            # have to let debugpy.listen() finish before we can attach message listeners (yes, I know
            # that the right way to do this is with a threading.Event(), or do the attaching of the listeners
            # inside debugPyListenerThreadTarget().  Sleeping is just a hack.

            # we have managed to reproduce much of the behavior of the fusion-ui-based launching of add-ins and scripts in debug mode,
            # but one thing we have not been able to reproduce is the behavior where clicking the "reload" button in VS code debugging interface
            # causes the add-in or script to run again.
            #also, we have not reprodfuced the behavior where fusion waits for the ide to connect to the debug adapter before running the 
            # script.  Somehow, we need to inject a wait-for-client function call just before running the script (but in another thread so as not to black
            # fusion's main thread), and somehow we need to catch the client refresh button press and re-launch jthe script in response.

            # perhaps there is something already established between fusion and the gloabl PyDB object such that I do not need
            # to bother running the script here, but rather can rely on this pre-established configuration to run the script merely as a result of
            # hitting F5 in vs code.
        except Exception:
            logger.fatal("An error occurred while attempting to start script.", exc_info=sys.exc_info())
        finally:
            pass

    def stop(self):
        if self._http_server:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                logger.error(f"Error while stopping {NAME_OF_THIS_ADDIN}'s HTTP server.", exc_info=sys.exc_info())
        self._http_server = None

        if self._rpyc_slave_server:
            try:
                self._rpyc_slave_server.close()
                # is it thread-safe to call the server's close() method here in a thread
                # other than the thread in which the server is running?
                # perhaps we need to .join(timeout=0) the thread in which the server is running and then 
                # run server's close() method.
            except Exception:
                logger.error(f"Error while stopping {NAME_OF_THIS_ADDIN}'s rpyc slave server.", exc_info=sys.exc_info())
        self._rpyc_slave_server = None

        del self._simpleFusionCustomCommands
        del self._fusionMainThreadRunner

        # clean up _logging_file_handler:
        try:
            if self._logging_file_handler:
                self._logging_file_handler.close()
                logger.removeHandler(self._logging_file_handler)
        except Exception:
            ui().messageBox(f"Error while closing {NAME_OF_THIS_ADDIN}'s file logger.\n\n%s" % traceback.format_exc())
        self._logging_file_handler = None

        # clean up _logging_dialog_handler:
        try:
            if self._logging_dialog_handler:
                self._logging_dialog_handler.close()
                logger.removeHandler(self._logging_dialog_handler)
        except Exception:
            ui().messageBox(f"Error while closing {NAME_OF_THIS_ADDIN}'s dialog logger.\n\n%s" % traceback.format_exc())
        self._logging_dialog_handler = None


        # clean up _logging_textcommands_palette_handler:
        try:
            if self._logging_textcommands_palette_handler:
                self._logging_textcommands_palette_handler.close()
                logger.removeHandler(self._logging_textcommands_palette_handler)
        except Exception:
            ui().messageBox(f"Error while closing {NAME_OF_THIS_ADDIN}'s textcommands palette logger.\n\n%s" % traceback.format_exc())
        self._logging_textcommands_palette_handler = None


def unload_submodules(module_name, prefixes_of_submodules_not_to_be_reloaded: 'list[str]'):
    search_prefix = module_name + '.'
    logger.debug(
        f"unloading modules whose name starts with {search_prefix}"
        + (
            "except those whose name starts with any of " + ", ".join((search_prefix + y for y in prefixes_of_submodules_not_to_be_reloaded))
            if prefixes_of_submodules_not_to_be_reloaded
            else ""
        )  
    )
    loaded_submodules_to_be_unloaded = []
    for loaded_module_name in sys.modules:
        # logger.debug(f"considering whether to unload {loaded_module_name}")
        if loaded_module_name.startswith(search_prefix) and not any( loaded_module_name.startswith(search_prefix + x) for x in  prefixes_of_submodules_not_to_be_reloaded ):
            loaded_submodules_to_be_unloaded.append(loaded_module_name)
    for loaded_submodule_to_be_unloaded in loaded_submodules_to_be_unloaded:
        logger.debug(f"unloading module {loaded_submodule_to_be_unloaded}")
        del sys.modules[loaded_submodule_to_be_unloaded]

def ensureThatDebuggingIsStarted(debugpy_path: str, debug_port: int) -> None:
    global debugpy
    global debugging_started

    # make sure that debugging is running.
    if not debugpy_path:
        logger.warning("We have been instructed to do debugging, but you have not provided the necessary debugpy_path.  Therefore, we can do nothing.")
        return
    initialSystemPath=sys.path.copy()
    sys.path.append(debugpy_path)
    import debugpy
    import debugpy._vendored
    with debugpy._vendored.vendored(project='pydevd'):
        from _pydevd_bundle.pydevd_constants import get_global_debugger
        from pydevd import PyDB
        import pydevd
    sys.path=initialSystemPath
    # I hope that it won't screw anything up to replace the sys.path value with a newly-created list (rather than modifying the existing list).
        
    
    if not debugging_started and get_global_debugger() is not None :  
        logger.debug("Our debugging_started flag is cleared, and yet the global debugger object exists (possibly left over from a previous run/stop cycle of this add in), so we will go ahead and set the debugging_started flag.")
        debugging_started = True  
        addin._simpleFusionCustomCommands.append(SimpleFusionCustomCommand(name="D_indicator", app=app(), logger=logger)) 
    # We are assuming that if the global debugger object exists, then debugging is active and configured as desired.
    # I am not sure that this is always a safe assumption, but oh well.

    # ensure that debugging is started:
    #  ideally, we would look for an existing global debugger with the correct configuration
    # in order to determine whether debugging was started, rather than maintaining our own blind debugging_started flag.
    # the problem is that our flag can be wrong in the case where this add-in was started with debugging already active (started
    # by a previous run/stop cycle of this add in).  
    # Also, we should be doing something to stop debugging when this add-in is stopped, rather than just leaving it running, which we are doing now.
    # It seems that pydevd, or, at least the parts of the pydevd behavior that debugpy exposes, is not geared toward stopping the debugging, only starting it.
    # what about pydevd.stoptrace() ? -- that's what Ben Gruver does.
    if not debugging_started:
        logger.debug("Commencing listening on port %d" % debug_port)
        
        # discovery: the text command "Python.IDE" configures and starts the debugpy adaptor process, just as happens when 
        # you use the Fusion UI to run a script in debug mode.  The text command also launches VS code.

        debugpy.configure(
            python= str(pathlib.Path(os.__file__).parents[1] / 'python')
            # this is a bit of a hack to get the path of the python executable that is bundled with Fusion.
        )

        # debugpy.listen(debug_port)
        (lambda : debugpy.listen(debug_port))()
        # the code-reachability analysis system that is built into VS code (is this Pylance?) falsely 
        # believes that the debugpy.listen()
        # function will always result in an exception, and therefore regards all code below this point
        # as unreachable, which causes vscode to display all code below this point in a dimmed color.
        # I find this so annoying that I have wrapped the debugpy.listen() in a lambda function
        # that I immediately call.  This seems to be sufficient to throw the code reachability analysis system 
        # off the scent, and hopefully will not change the effect of the code.
    
        class LoggingDapMessagesListener(pydevd.IDAPMessagesListener):
            # @overrides(pydevd.IDAPMessagesListener.after_receive)
            def after_receive(self, message_as_dict):
                logger.debug(f"LoggingDapMessagesListener::after_receive({message_as_dict})")
            
            def before_send(self, message_as_dict):
                logger.debug(f"LoggingDapMessagesListener::before_send({message_as_dict})")  
        if False:
            pydevd.add_dap_messages_listener(LoggingDapMessagesListener())
        
        #display a "D" button in the quick-access toolbar as a visual indicator to the user that debugging is now active.
        debugging_started = True
        addin._simpleFusionCustomCommands.append(SimpleFusionCustomCommand(name="D_indicator", app=app(), logger=logger))

class FusionErrorDialogLoggingHandler(logging.Handler):
    """A logging handler that shows a error dialog to the user in Fusion 360."""

    def __init__(self):
        super().__init__()
        # we use our own private instance of FusionMainThreadRunner rather than some externally-created instance
        # because we want our instance's logger NOT to be the same logger for which we are a handler,
        # else we might have infinite loops while logging.
        self._fusionMainThreadRunner = fusion_main_thread_runner.FusionMainThreadRunner()

    def emit(self, record: logging.LogRecord) -> None:
        self._fusionMainThreadRunner.doTaskInMainFusionThread(
            lambda : ui().messageBox(self.format(record), f"{NAME_OF_THIS_ADDIN} error")
        )

class FusionTextCommandsPalletteLoggingHandler(logging.Handler):
    """A logging handler that writes log messages to the Fusion TextCommands palette."""

    def __init__(self):
        super().__init__()
        # we use our own private instance of FusionMainThreadRunner rather than some externally-created instance
        # because we want our instance's logger NOT to be the same logger for which we are a handler,
        # else we might have infinite loops while logging.
        self._fusionMainThreadRunner = fusion_main_thread_runner.FusionMainThreadRunner()

    def emit(self, record: logging.LogRecord) -> None:

        # we need to have a way to ensure that, while the fusionMainThreadRunner is running this task,
        # that no loggin calls occur, or at least that, if the running of the task by fusionMainThreadRunner 
        # does cause a logging record to be emitted, that we do not call doTaskInMainFusionThread again.
        # The goal is to avoid an infinite loop, wherein the act of logging a message itself causes another 
        # message to be logged.
        # addin._fusionMainThreadRunner.doTaskInMainFusionThread(
        #     lambda : 
        #         ui().palettes.itemById('TextCommands').writeText(self.format(record)),
        #     suppressLogging=True
        # )
        self._fusionMainThreadRunner.doTaskInMainFusionThread(
            lambda : 
                ui().palettes.itemById('TextCommands').writeText(self.format(record))
        )
        # We do not want the logging system to rely on fusionMainThreadRunner, because fusionMainThreadRunner might itself use
        # the logging system.  Therefore, we will manually set up the fusion custom event and associated handler here, rather than relying on 
        # the equivalent functionality in fusionMainThreadRunner.


    


class RunScriptHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """An HTTP request handler that queues an event in the main thread of fusion 360 to run a script."""

    def do_POST(self):
        logger.debug("Got an http request.")
        content_length = int(self.headers["Content-Length"])
        body = self.rfile.read(content_length).decode()

        try:
            # logger.debug("RunScriptHTTPRequestHandler::do_POST is running with body " + body)
            request_json = json.loads(body)
            logger.debug("RunScriptHTTPRequestHandler::do_POST is running with request_json " + json.dumps(request_json))
            # logger.debug("type(request_json['message']): " + str(type(request_json['message'])))
 
            # It seems clunky to require that request_json["message"] be a string.  I think it makes more sense 
            # to allow it to be 
            # an object (in which case we need to stringify it before passing it to fireCustomEvent
            # because fireCustomEvent requires a string for its 'addionalInfo' argument.), but also 
            # handle the case where it is a string
            # (in which case we assume that it is the json-serialized version of the object.)

            # app().fireCustomEvent( 
            #     RUN_SCRIPT_REQUESTED_EVENT_ID,  
            #     # request_json["message"]
            #     ( request_json['message'] if isinstance(request_json['message'], str) else json.dumps(request_json['message']))
            # )

            # additionalInfo (the second argument to fireCustomeEvent()) is a string that will be retrievable in the notify(args) method of the 
            # customEventHandler
            # as args.additionalInfo 

            message = ( json.loads(request_json['message']) if isinstance(request_json['message'], str) else request_json['message'])
            # we ought to do some validation of the contents of message here and produce a meaningful error message
            # to the caller if arguments are not as expected.

            addin._fusionMainThreadRunner.doTaskInMainFusionThread(
                lambda : addin.runScript(
                    script_path     = message.get("script"),
                    debug           = bool(message.get("debug")),
                    debugpy_path    = message.get("debugpy_path"),
                    debug_port      = int(message.get("debug_port",0)),
                    prefixes_of_submodules_not_to_be_reloaded = message.get("prefixes_of_submodules_not_to_be_reloaded")
                )
            )

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"done")
        except Exception:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(traceback.format_exc().encode())
            logger.error("An error occurred while handling http request.", exc_info=sys.exc_info())

addin = AddIn()

def run(context:dict):
    addin.start()

def stop(context:dict):
    logger.debug("stopping")
    addin.stop()
