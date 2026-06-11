import logging
from flask import jsonify

_routes: dict[str, dict] = {}


def register_plugin_route(plugin_id: str, path: str, handler):
    _routes.setdefault(plugin_id, {})[path] = handler


def get_all_routes(plugin_id: str) -> dict:
    return _routes.get(plugin_id, {})


def make_view(handler):
    """Wrap a plain plugin handler into a Flask view function."""
    def view(*args, **kwargs):
        try:
            result = handler()
        except Exception as e:
            logging.exception(f"Plugin handler error: {e}")
            return jsonify({'error': str(e)}), 500
        if isinstance(result, tuple):
            body, status = result[0], result[1]
            return jsonify(body), status
        return jsonify(result)
    return view
