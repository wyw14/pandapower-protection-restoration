"""The application exchanges native pandapower JSON, not a reduced branch table."""
import pandapower as pp


def load_network(text):
    if not isinstance(text, str):
        raise ValueError('network_json must be a pandapower JSON string')
    try:
        return pp.from_json_string(text)
    except Exception as exc:
        raise ValueError('network_json cannot be loaded') from exc
