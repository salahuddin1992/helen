"""
Socket.IO module — import all handlers to register them with the sio server.
"""

from app.socket.server import sio  # noqa: F401
from app.socket.auth_handlers import *  # noqa: F401, F403
from app.socket.presence_handlers import *  # noqa: F401, F403
from app.socket.chat_handlers import *  # noqa: F401, F403
from app.socket.call_handlers import *  # noqa: F401, F403
from app.socket.sync_handlers import *  # noqa: F401, F403
from app.socket.screen_handlers import *  # noqa: F401, F403
from app.socket.notification_handlers import *  # noqa: F401, F403
from app.socket.voice_handlers import *  # noqa: F401, F403
from app.socket.e2ee_handlers import *  # noqa: F401, F403
from app.socket.whiteboard_handlers import *  # noqa: F401, F403
from app.socket.file_drop_handlers import *  # noqa: F401, F403
from app.socket.group_file_handlers import *  # noqa: F401, F403
from app.socket.transport_handlers import *  # noqa: F401, F403
from app.socket.topology_handlers import *  # noqa: F401, F403
from app.socket.pair_handlers import *  # noqa: F401, F403
