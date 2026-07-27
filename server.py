import asyncio
import json
import logging
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MeditationServer")

connected_players: dict[str, WebSocketServerProtocol] = {}
player_states = {
    "p1": {"raw": 0.0, "normalized": 0.0, "calibrating": False},
    "p2": {"raw": 0.0, "normalized": 0.0, "calibrating": False}
}

async def register(websocket: WebSocketServerProtocol, player_id: str):
    connected_players[player_id] = websocket
    logger.info(f"Player {player_id.upper()} connected from {websocket.remote_address}")

async def unregister(player_id: str):
    if player_id in connected_players:
        del connected_players[player_id]
        logger.info(f"Player {player_id.upper()} disconnected")

async def handler(websocket):
    path = websocket.request.path
    path_parts = path.strip("/").split("/")
    if len(path_parts) < 2 or path_parts[0] != "ws" or path_parts[1] not in ["p1", "p2"]:
        await websocket.close(1008, "Invalid path. Use /ws/p1 or /ws/p2")
        return

    player_id = path_parts[1]
    await register(websocket, player_id)

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                
                if data.get("event") == "calibration_complete":
                    logger.info(f"PLAYER {player_id.upper()} CALIBRATED. Baseline: {data.get('baseline'):.4f}, HalfRange: {data.get('halfRange'):.4f}")
                    continue

                player_states[player_id] = {
                    "raw": data.get("rawScore", 0.0),
                    "normalized": data.get("normalizedScore", 0.0),
                    "calibrating": data.get("isCalibrating", False)
                }

                # Clear line and print live values
                print(f"\rP1: {player_states['p1']['normalized']:+0.2f} | P2: {player_states['p2']['normalized']:+0.2f}", end="", flush=True)

            except json.JSONDecodeError:
                pass
    except websockets.exceptions.ConnectionClosedError:
        pass
    finally:
        await unregister(player_id)

async def main():
    # Run server on port 3000
    server = await websockets.serve(handler, "0.0.0.0", 3000)
    logger.info("WebSocket Server listening on ws://localhost:3000")
    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped.")
