from PIL import Image
from PIL import ImageFont
from PIL import ImageDraw 
import os
from typing import Optional, Callable
import adsk.core
import adsk.fusion
import datetime 
import tempfile
import logging
import sys


class SimpleFusionCustomCommand(object):
    """ This class automates the housekeeping involved with creating a custom command linked to a toolbar button in Fusion """
    # Given the way Fusion uses the word "command" to refer to an action currently in progress rather than 
    # a meaning more closely aligned with my intuitive notion of command, which is something like "function",
    # it might make sense not to name this class "command", but use some other term like "function" "task" "procedure" "routine" etc.

    def __init__(self, 
        name: str, 
        app: adsk.core.Application, 
        action: Optional[ Callable[[adsk.core.CommandEventArgs] , None] ] = None,  
        logger: Optional[logging.Logger] = None
    ):
        self._name = name
        self._action = action or self.doNothingAction
        self._commandId = self._name # TO-DO: ensure that the commandId is unique and doesn't contain any illegal characters.
        self._app = app
        self._logger = logger
        self._resourcesDirectory = tempfile.TemporaryDirectory()
        self._logger and self._logger.debug("self._resourcesDirectory.name: " + self._resourcesDirectory.name)
        self._logger and self._logger.debug("sys.version: " + sys.version)

        iconText = self._name[0].capitalize()
        for imageSize in (16, 32, 64):
            img = Image.new(mode='RGB',size=(imageSize, imageSize),color='white')
            draw :ImageDraw.ImageDraw = ImageDraw.Draw(img)
            font = ImageFont.truetype("arial.ttf",imageSize)
            draw.text((imageSize/2,imageSize/2),iconText ,font=font, fill='black',anchor='mm')
            img.save(os.path.join(self._resourcesDirectory.name, f"{imageSize}x{imageSize}.png"))

        self._commandDefinition = self._app.userInterface.commandDefinitions.addButtonDefinition(
            #id=
            self._commandId,
            #name=
            self._name,
            #tootlip=
            self._name,
            #resourceFolder(optional)=
            # (i'm omitting the resourceFolder argument for now)
            self._resourcesDirectory.name
        )
        self._commandCreatedHandler = self.CommandCreatedEventHandler(owner=self)
        self._commandDefinition.commandCreated.add(self._commandCreatedHandler)
        self._commandEventHandler = self.CommandEventHandler(owner=self)
        self._toolbarControl : adsk.core.CommandControl = self._app.userInterface.toolbars.itemById('QAT').controls.addCommand(self._commandDefinition)
        self._toolbarControl.isVisible = True

    def __del__(self):
        self._commandDefinition.deleteMe()
        del self._commandDefinition
        self._toolbarControl.deleteMe()
        del self._toolbarControl
        self._resourcesDirectory.cleanup()
        del self._resourcesDirectory


    def doNothingAction(self, eventArgs: adsk.core.CommandEventArgs) -> None:
        self._app.userInterface.palettes.itemById('TextCommands').writeText(str(datetime.datetime.now()) + "\t" + 'Hello doNothing from ' + __file__)

    class CommandCreatedEventHandler(adsk.core.CommandCreatedEventHandler):
        def __init__(self, owner: 'SimpleFusionCustomCommand'):
            super().__init__()
            self._owner = owner
        def notify(self, args: adsk.core.CommandCreatedEventArgs):
            args.command.execute.add(self._owner._commandEventHandler)
            args.command.destroy.add(self._owner._commandEventHandler)
            args.command.executePreview.add(self._owner._commandEventHandler)

    class CommandEventHandler(adsk.core.CommandEventHandler):
        def __init__(self, owner: 'SimpleFusionCustomCommand'):
            super().__init__()
            self._owner = owner
        def notify(self, args: adsk.core.CommandEventArgs):    
            if args.firingEvent.name == 'OnExecute':
                self._owner._action(args)
