"""Reader for the central config.env (see config.env.example in the repo root)."""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_cache = None


def config_path():
    for c in (os.environ.get("LOCALAI_CONFIG"), os.path.join(_HERE, "config.env"),
              os.path.join(os.path.dirname(_HERE), "config.env")):
        if c and os.path.isfile(c):
            return c
    return None


def load():
    global _cache
    if _cache is None:
        _cache = {}
        p = config_path()
        if p:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        _cache[k.strip()] = v.strip()
    return _cache


def get(key, default=""):
    v = load().get(key, "")
    return v if v != "" else default


def base_url():
    return "http://{}:{}".format(get("HOST", "127.0.0.1"), get("PROXY_PORT", "8080"))
