#!/usr/bin/env python3
"""
Web UI for Agent

Simple Flask-based web interface for chatting with the agent.
Provides a chat interface with Server-Sent Events for streaming responses.
"""

import argparse
import json
import threading
from uuid import uuid4
from flask import Flask, request, jsonify, Response, render_template

from .agent import ToolCallingAgent
from .tool_manager import ToolManager

# Initialize Flask app
app = Flask(__name__)

# Global state
tool_manager = None
agent = None
mcp_server_status = []
agent_lock = threading.Lock()
pending_confirmation_lock = threading.Lock()
pending_confirmations = {}


def create_agent():
    """Create a new agent instance with the current tool manager."""
    global agent
    agent = ToolCallingAgent(tool_manager)


@app.route('/')
@app.route('/index.html')
def index():
    """Serve the HTML interface."""
    return render_template('webui.html')


@app.route('/api/chat', methods=['POST'])
def chat():
    """Handle chat messages with Server-Sent Events streaming."""
    import queue
    import threading

    data = request.json
    message = data.get('message', '')

    if not message:
        return jsonify({'error': 'No message provided'}), 400

    # Queue to communicate between threads
    event_queue = queue.Queue()

    def run_agent():
        """Run agent in separate thread and put results in queue."""
        import sys
        import io

        def confirm_action(tool_name, arguments):
            confirmation_id = uuid4().hex
            confirmation_event = threading.Event()
            confirmation = {
                "event": confirmation_event,
                "approved": False,
                "decided": False,
            }
            with pending_confirmation_lock:
                pending_confirmations[confirmation_id] = confirmation
            event_queue.put(
                (
                    "confirmation",
                    {
                        "confirmation_id": confirmation_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    },
                )
            )
            approved = confirmation_event.wait(timeout=300)
            with pending_confirmation_lock:
                pending_confirmations.pop(confirmation_id, None)
            return approved and confirmation["approved"]

        def show_receipt(receipt):
            event_queue.put(("action_receipt", receipt))

        # Capture stdout to intercept rate limit messages
        class OutputCapture(io.StringIO):
            def write(self, text):
                if '⏱️' in text:
                    # Send waiting event immediately
                    event_queue.put(('waiting', text.strip()))
                return super().write(text)

        try:
            with agent_lock:
                old_stdout = sys.stdout
                sys.stdout = OutputCapture()
                agent.confirmation_callback = confirm_action
                agent.receipt_callback = show_receipt
                try:
                    response = agent.execute(message)
                finally:
                    agent.confirmation_callback = None
                    agent.receipt_callback = None
                    sys.stdout = old_stdout
            event_queue.put(('response', response))
        except Exception as error:
            event_queue.put(('error', str(error)))
        finally:
            event_queue.put(('done', None))

    def generate():
        """Generator for Server-Sent Events."""
        # Start agent in background thread
        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()

        # Stream events as they come from the queue
        while True:
            event_type, data = event_queue.get()

            if event_type == 'waiting':
                yield f"event: waiting\ndata: {json.dumps({'message': data})}\n\n"
            elif event_type == 'response':
                yield f"event: response\ndata: {json.dumps({'message': data})}\n\n"
            elif event_type == 'error':
                yield f"event: error\ndata: {json.dumps({'message': data})}\n\n"
            elif event_type == 'confirmation':
                yield f"event: confirmation\ndata: {json.dumps(data)}\n\n"
            elif event_type == 'action_receipt':
                yield f"event: action_receipt\ndata: {json.dumps(data)}\n\n"
            elif event_type == 'done':
                yield f"event: done\ndata: {{}}\n\n"
                break

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/confirm', methods=['POST'])
def confirm_action():
    """Record the user's decision for a pending action confirmation."""
    data = request.get_json(silent=True) or {}
    confirmation_id = data.get("confirmation_id")
    approved = data.get("approved")
    if not isinstance(confirmation_id, str) or not isinstance(approved, bool):
        return jsonify({"error": "confirmation_id and approved are required"}), 400

    with pending_confirmation_lock:
        confirmation = pending_confirmations.get(confirmation_id)
        if confirmation is None:
            return jsonify({"error": "Confirmation is missing or expired"}), 404
        if confirmation["decided"]:
            return jsonify({"error": "A decision was already recorded"}), 409
        confirmation["approved"] = approved
        confirmation["decided"] = True
        confirmation["event"].set()
    return jsonify({"success": True})


@app.route('/api/mcp-servers', methods=['GET'])
def get_mcp_servers():
    """Get MCP server status."""
    return jsonify(mcp_server_status)


@app.route('/api/action-history', methods=['POST'])
def get_action_history():
    """Fetch server-authored action receipts without asking the model to summarize them."""
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        raw_actions = tool_manager.execute_tool(
            "get_action_history", {"user_id": user_id}
        )
        actions = json.loads(raw_actions)
        if not isinstance(actions, list):
            raise ValueError("Airline server returned an invalid action history")
        return jsonify({"actions": actions})
    except Exception as error:
        return jsonify({"error": str(error)}), 502


@app.route('/api/reset', methods=['POST'])
def reset():
    """Reset the agent conversation."""
    with agent_lock:
        create_agent()
    return jsonify({'success': True})


def main():
    """Main entry point for the web UI."""
    parser = argparse.ArgumentParser(
        description="Run agent web UI with MCP servers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  agent-webui "http://localhost:3000/mcp"
        """
    )

    parser.add_argument(
        'mcp_servers',
        nargs='*',
        help='MCP server URLs or commands'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=8000,
        help='Port to run the web server on (default: 8000)'
    )

    args = parser.parse_args()

    # Initialize global tool manager
    global tool_manager, mcp_server_status
    tool_manager = ToolManager.from_servers(args.mcp_servers)

    # Get server status
    statuses = tool_manager.get_server_status()
    mcp_server_status = [
        {
            'config': status.config,
            'transport': status.transport,
            'status': status.status,
            'error': status.error
        }
        for status in statuses
    ]

    # Create initial agent
    create_agent()

    # Start server
    print(f"\n🌐 Server running at http://localhost:{args.port}\n")
    app.run(host='0.0.0.0', port=args.port, debug=False)


if __name__ == '__main__':
    main()
