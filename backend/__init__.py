"""revit-bridge-web backend: a thin FastAPI host around the revit_bridge package.

No domain logic lives here. Revit access, capability packs, the sandbox and
slot-token checks all come from ``revit_bridge``; this package adds the HTTP
surface, the WebSocket relay for remote add-ins, a bring-your-own-model chat
stream, the skill directory and interaction logs.
"""
