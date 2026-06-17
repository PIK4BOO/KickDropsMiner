"""Small JSON helpers inspired by TwitchDropsMiner's settings utilities."""
import copy
import json
import os
import tempfile


def merge_defaults(data, defaults):
    """Return data merged into defaults, keeping only known keys and expected types."""
    if not isinstance(data, dict):
        return copy.deepcopy(defaults)

    merged = copy.deepcopy(defaults)
    for key, default_value in defaults.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(default_value, dict):
            merged[key] = merge_defaults(value, default_value)
        elif isinstance(default_value, list):
            merged[key] = value if isinstance(value, list) else copy.deepcopy(default_value)
        elif default_value is None:
            merged[key] = value
        elif isinstance(value, type(default_value)):
            merged[key] = value
    return merged


def json_load(path, defaults=None, merge=True):
    """Load JSON from path, optionally merged with defaults."""
    new_path = f"{path}.new"
    data = None

    if os.path.exists(new_path):
        try:
            with open(new_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.replace(new_path, path)
        except (OSError, json.JSONDecodeError):
            try:
                os.remove(new_path)
            except OSError:
                pass

    if data is None and not os.path.exists(path):
        return copy.deepcopy(defaults) if defaults is not None else {}

    if data is None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return copy.deepcopy(defaults) if defaults is not None else {}

    if defaults is not None and merge:
        return merge_defaults(data, defaults)
    return data


def json_save_atomic(path, data, indent=2):
    """Save JSON through a recoverable .new file then replace the target."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json.new", dir=directory)
    new_path = f"{path}.new"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, new_path)
        os.replace(new_path, path)
    except Exception:
        for cleanup_path in (tmp_path, new_path):
            try:
                os.remove(cleanup_path)
            except OSError:
                pass
        raise
