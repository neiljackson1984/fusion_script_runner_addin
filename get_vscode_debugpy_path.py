# This script attempts to find the path of the vscode's python plug-in's debugpy package.
# This script is useful to automate the debugging process for Fusion Python scripts.

import sys
import os
import re


def locatePythonToolFolder():

    vscodeExtensionPath = ''
    if sys.platform.startswith('win'):
        vscodeExtensionPath = os.path.expandvars(r'%USERPROFILE%\.vscode\extensions')
    else:
        vscodeExtensionPath = os.path.expanduser('~/.vscode/extensions')

    if os.path.exists(vscodeExtensionPath) == False:
        return ''

    msPythons = []
    versionPattern = re.compile(r'ms-python.python-(?P<major>\d+).(?P<minor>\d+).(?P<patch>\d+)')
    for entry in os.scandir(vscodeExtensionPath):
        if entry.is_dir(follow_symlinks=False):
            match = versionPattern.match(entry.name)
            if match:
                try:
                    version = tuple(int(match[key]) for key in ('major', 'minor', 'patch'))
                    msPythons.append((entry, version))
                except:
                    pass

    msPythons.sort(key=lambda pair: pair[1], reverse=True)
    if (msPythons):
        if None == msPythons[0]:
            return ''
        msPythonPath = os.path.expandvars(msPythons[0][0].path)
        index = msPythonPath.rfind('.')
        version  = int(msPythonPath[index+1:])
        msPythonPath = os.path.join(msPythonPath, 'pythonFiles', 'lib','python')
        msPythonPath = os.path.normpath(msPythonPath)
        if os.path.exists(msPythonPath) and os.path.isdir(msPythonPath):
            return msPythonPath
    return ''

print(locatePythonToolFolder())




