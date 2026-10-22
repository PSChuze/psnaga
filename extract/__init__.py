"""Build the server's game data from your own Nobunaga no Yabou Online - Hiryuu no Shou disc.

python -m extract --disc <disc image or folder> --out <assets folder>

Standard library only. Nothing from the disc is kept beyond what the server reads.
"""
import os
import sys

# In a clone the server package lives in server/; in the image it sits next to this one.
_server = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if os.path.isdir(os.path.join(_server, "nobu")) and _server not in sys.path:
    sys.path.insert(0, _server)
