"""Stage 1 — deterministic speech normalization.

Speak the meaning, not the serialization. This pass is fast, has no
network, and must never raise: unknown constructs stay as readable
source text rather than blocking TTS.
"""

from __future__ import annotations

import html
import re
from typing import Callable, List, Tuple

from .config import HumanizeConfig


# ---------------------------------------------------------------------------
# Number / unit speaking
# ---------------------------------------------------------------------------

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
)
_TEENS = (
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
)

_UNIT_WORDS = {
    "ms": "milliseconds",
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "kb": "kilobytes",
    "mb": "megabytes",
    "gb": "gigabytes",
    "tb": "terabytes",
    "kib": "kibibytes",
    "mib": "mebibytes",
    "gib": "gibibytes",
    "b": "bytes",
    "px": "pixels",
}


def speak_int(n: int) -> str:
    """Cardinal English for a reasonably-sized integer."""
    if n < 0:
        return "minus " + speak_int(-n)
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        if rest == 0:
            return f"{_ONES[hundreds]} hundred"
        # 101 → one hundred one; 401 as a count is "four hundred one"
        glue = " and " if rest < 10 else " "
        return f"{_ONES[hundreds]} hundred{glue}{speak_int(rest)}"
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = speak_int(thousands) + " thousand"
        return head if rest == 0 else f"{head} {speak_int(rest)}"
    return str(n)


def speak_issue_number(n: int) -> str:
    """GitHub-style: 123 → 'one twenty-three', 191 → 'one ninety-one'."""
    if n < 100:
        return speak_int(n)
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        if rest == 0:
            return f"{speak_int(hundreds)} hundred"
        return f"{speak_int(hundreds)} {speak_int(rest)}"
    return speak_int(n)


def speak_http_code(code: int) -> str:
    s = f"{code:03d}"
    if s[1] == "0" and s[2] != "0":
        return f"{speak_int(int(s[0]))}-oh-{speak_int(int(s[2]))}"
    return speak_int(code)


def speak_number_token(raw: str) -> str:
    raw = raw.replace(",", "")
    if re.fullmatch(r"\d+", raw):
        try:
            return speak_int(int(raw))
        except ValueError:
            return raw
    if re.fullmatch(r"\d+\.\d+", raw):
        whole, frac = raw.split(".", 1)
        if set(frac) <= {"0"}:
            return speak_int(int(whole))
        return f"{speak_int(int(whole))} point {' '.join(_ONES[int(d)] for d in frac)}"
    return raw


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

_ACRONYMS = {
    "API", "URL", "URI", "HTTP", "HTTPS", "HTML", "CSS", "JSON", "YAML", "TOML",
    "XML", "SQL", "JWT", "SSH", "SSL", "TLS", "TCP", "UDP", "DNS", "IP", "ID",
    "UUID", "SHA", "TTS", "STT", "CLI", "CPU", "GPU", "RAM", "OS", "UI", "UX",
    "DB", "PR", "QA", "CI", "CD", "ENV", "NPM", "AWS", "GCP", "SDK", "OTP",
    "SSO", "IAM", "CDN", "SSE", "LLM", "PNG", "JPG", "SVG", "PDF", "WAV", "MP3",
    "OK", "CRUD", "REST", "RPC", "GRPC", "ASCII", "UTF",
}

_PROPER = {
    "OPENAI": "OpenAI",
    "ANTHROPIC": "Anthropic",
    "GITHUB": "GitHub",
    "GITLAB": "GitLab",
    "DOCKER": "Docker",
    "KUBERNETES": "Kubernetes",
    "NODE": "Node",
    "NODEJS": "Node.js",
    "TYPESCRIPT": "TypeScript",
    "JAVASCRIPT": "JavaScript",
    "PYTHON": "Python",
    "POSTGRES": "Postgres",
    "REDIS": "Redis",
    "RETINUE": "Retinue",
    "HERMES": "Hermes",
    "NOVIQUE": "Novique",
    "CHATGPT": "ChatGPT",
    "GROK": "Grok",
    "XAI": "xAI",
    "AUTH": "auth",
    "README": "readme",
}

_EXT_LANG = {
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".py": "Python",
    ".rb": "Ruby",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".css": "CSS",
    ".html": "HTML",
    ".md": "markdown",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".sql": "SQL",
    ".json": None,  # spoken as "dot json"
}

_SKIP_PATH_PARENTS = {
    "src", "lib", "libs", "app", "apps", "test", "tests", "script", "scripts",
    "bin", "dist", "build", "doc", "docs", "tool", "tools", "package",
    "packages", "plugin", "plugins", "node_modules", "vendor", "home",
    "users", "tmp", "var", "opt", "usr", "etc",
}


def split_ident(token: str) -> str:
    """Speak an identifier as words. OPENAI_API_KEY → 'OpenAI API key'."""
    if not token:
        return token
    token = token.strip("${}")
    if token.startswith("process.env."):
        token = token[len("process.env."):]
    parts: List[str] = []
    if "_" in token or (token.isupper() and "-" not in token):
        chunks = [p for p in re.split(r"[_\-]+", token) if p]
    else:
        chunks = re.findall(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+", token)
        if not chunks:
            chunks = [token]
    for chunk in chunks:
        upper = chunk.upper()
        if upper in _PROPER:
            parts.append(_PROPER[upper])
        elif upper in _ACRONYMS:
            parts.append(upper if upper in {"API", "URL", "HTTP", "HTTPS", "ID", "JSON", "TTS", "STT", "UI", "UX", "DB", "PR", "QA", "CI", "CD", "OK", "IP", "SHA", "CLI", "SQL", "HTML", "CSS", "XML", "JWT", "SSH", "SSL", "TLS", "CPU", "GPU", "RAM", "DNS", "SDK", "SSE", "LLM", "PDF"} else upper)
        elif chunk.isdigit():
            parts.append(speak_int(int(chunk)))
        else:
            parts.append(chunk.lower())
    spoken = " ".join(parts)
    spoken = re.sub(r"\bapi key\b", "API key", spoken, flags=re.I)
    spoken = re.sub(r"\bbase url\b", "base URL", spoken, flags=re.I)
    return spoken


def _filename_or_raw(bank: "_Bank", name: str) -> str:
    """Skip tokens that only look like files (process.env, Node.js)."""
    lower = name.lower()
    if lower in {"process.env", "node.js"}:
        return name
    return bank.stash(_humanize_filename(name))


def _speak_small_int(m: re.Match[str]) -> str:
    n = int(m.group(1))
    if 1900 <= n <= 2100:
        return m.group(1)
    return speak_int(n)


def _humanize_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    lower = base.lower()
    if lower in {"readme", "readme.md", "readme.txt"}:
        return "the readme"
    if lower in {"dockerfile"}:
        return "the dockerfile"
    if lower in {".gitignore", "gitignore"}:
        return "the gitignore file"
    if lower in {"makefile"}:
        return "the makefile"
    stem, ext = (base, "")
    if "." in base and not base.startswith("."):
        stem, ext = base.rsplit(".", 1)
        ext = "." + ext
    elif base.startswith(".") and base.count(".") >= 1:
        stem, ext = base, ""
    spoken_stem = split_ident(stem)
    lang = _EXT_LANG.get(ext.lower())
    if ext.lower() == ".json":
        return f"{spoken_stem} dot json"
    if ext.lower() == ".md" and stem.lower() == "readme":
        return "the readme"
    if lang:
        return f"the {spoken_stem} {lang} file"
    if ext:
        return f"{spoken_stem} dot {ext[1:]}"
    return spoken_stem


def humanize_path(path: str) -> str:
    path = path.strip().strip("`\"'")
    path = path.replace("\\", "/")
    if not path:
        return path
    # Bare filename.
    if "/" not in path.strip("/"):
        return _humanize_filename(path)
    parts = [p for p in path.split("/") if p and p not in {".", "..", "~"}]
    if not parts:
        return "the file"
    name = parts[-1]
    parent = parts[-2] if len(parts) >= 2 else ""
    spoken_name_stem = split_ident(name.rsplit(".", 1)[0] if "." in name and not name.startswith(".") else name)
    if parent and parent.lower() not in _SKIP_PATH_PARENTS and not parent.startswith("."):
        return f"the {split_ident(parent)} {spoken_name_stem} file"
    return f"the {_humanize_filename(name).removeprefix('the ')}"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def humanize_url(url: str) -> str:
    """Point at a URL; never read host, path, query, or GitHub numbers.

    The canonical reply still shows the link. TTS says "this link".
    """
    return "this link"


# ---------------------------------------------------------------------------
# Commands / code fences
# ---------------------------------------------------------------------------

_CMD_HINTS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^docker\s+compose\s+down\b", re.I), "stop the services"),
    (re.compile(r"^docker\s+compose\s+up\b.*--build", re.I), "rebuild and restart them"),
    (re.compile(r"^docker\s+compose\s+up\b", re.I), "start the services"),
    (re.compile(r"^docker\s+compose\s+logs\b", re.I), "follow the logs"),
    (re.compile(r"^docker\s+compose\s+ps\b", re.I), "list the containers"),
    (re.compile(r"^npm\s+install\b", re.I), "npm install"),
    (re.compile(r"^npm\s+(test|run\s+test)\b", re.I), "run the tests"),
    (re.compile(r"^npm\s+run\s+lint\b", re.I), "run the linter"),
    (re.compile(r"^npm\s+run\s+build\b", re.I), "run the build"),
    (re.compile(r"^pnpm\s+install\b", re.I), "pnpm install"),
    (re.compile(r"^yarn\s+(test|install)\b", re.I), None),  # keep command name
    (re.compile(r"^pytest\b", re.I), "run the tests"),
    (re.compile(r"^cargo\s+test\b", re.I), "run the tests"),
    (re.compile(r"^git\s+status\b", re.I), "git status"),
    (re.compile(r"^git\s+diff\b", re.I), "git diff"),
    (re.compile(r"^git\s+push\b", re.I), "git push"),
    (re.compile(r"^git\s+pull\b", re.I), "git pull"),
    (re.compile(r"^curl\b.*localhost.*health", re.I), "check the local health endpoint"),
    (re.compile(r"^curl\b", re.I), "run a curl request"),
)


def _humanize_git_checkout(cmd: str) -> str | None:
    m = re.match(r"git\s+checkout\s+-b\s+(\S+)", cmd.strip(), re.I)
    if not m:
        return None
    branch = m.group(1).strip()
    pretty = split_ident(branch.replace("/", "_").replace("-", "_"))
    return f"create a new Git branch for {pretty}"


def humanize_command(cmd: str) -> str:
    cmd = cmd.strip().strip("`")
    if not cmd:
        return ""
    git = _humanize_git_checkout(cmd)
    if git:
        return git
    for pat, spoken in _CMD_HINTS:
        if pat.search(cmd):
            if spoken is None:
                break
            return spoken
    # Short command: drop flags, keep the verb + target.
    tokens = cmd.split()
    kept: List[str] = []
    for tok in tokens:
        if tok.startswith("-"):
            continue
        if tok in {"&&", "||", "|", ";"}:
            break
        kept.append(tok)
        if len(kept) >= 3:
            break
    if kept:
        return " ".join(kept)
    return "a command on screen"


def summarize_code_fence(info: str, body: str) -> str:
    lang = (info or "").split()[0].lower() if info else ""
    if lang in {"itinerary", "json"}:
        if lang == "json" or _looks_like_json(body):
            return _summarize_json(body)
        return ""
    lines = [
        ln.strip()
        for ln in body.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return ""
    if len(lines) == 1 and len(lines[0]) < 80 and not _looks_like_json(lines[0]):
        return humanize_command(lines[0])
    purposes = [humanize_command(ln) for ln in lines]
    purposes = [p for p in purposes if p]
    n = len(lines)
    kind = "Docker" if any("docker" in ln.lower() for ln in lines) else (
        "Git" if any(ln.lower().startswith("git ") for ln in lines) else "shell"
    )
    if n == 1:
        return f"I've included a {kind} command on screen to {purposes[0]}." if purposes else (
            f"I've included a {kind} command on screen."
        )
    if purposes and len(purposes) == n:
        if n == 2:
            joined = f"{purposes[0]} and {purposes[1]}"
        else:
            joined = ", ".join(purposes[:-1]) + f", and then {purposes[-1]}"
        return f"I've included {speak_int(n)} {kind} commands on screen to {joined}."
    return f"I've included {speak_int(n)} {kind} commands on screen."


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    return (stripped.startswith("{") and ":" in stripped) or (
        stripped.startswith("[") and stripped.endswith("]")
    )


def _summarize_json(body: str) -> str:
    stripped = body.strip()
    m = re.search(r'["\']status["\']\s*:\s*["\']([^"\']+)["\']', stripped)
    if m and stripped.count(":") <= 3:
        return f"JSON with status {m.group(1)}"
    return "JSON output on screen"


# ---------------------------------------------------------------------------
# Token bank — protect already-spoken spans from later regexes
# ---------------------------------------------------------------------------

class _Bank:
    def __init__(self) -> None:
        self._items: List[str] = []

    def stash(self, spoken: str) -> str:
        key = f"\x01H{len(self._items)}\x02"
        self._items.append(spoken)
        return key

    def restore(self, text: str) -> str:
        for i, spoken in enumerate(self._items):
            text = text.replace(f"\x01H{i}\x02", spoken)
        return text


# ---------------------------------------------------------------------------
# Regex catalogue
# ---------------------------------------------------------------------------

_ITINERARY_FENCE_RE = re.compile(r"```+\s*itinerary\b[\s\S]*?(?:```+|$)", re.I)
_CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n([\s\S]*?)```")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s)<>\"']+")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HTTP_STATUS_RE = re.compile(r"\bHTTP\s*([1-5]\d{2})\b", re.I)
_HTTP_METHOD_PATH_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%{}-]+)",
    re.I,
)
_TEST_RATIO_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\s+passed\b", re.I)
_ERR_WARN_RE = re.compile(
    r"\b(\d+)\s+errors?\s*,\s*(\d+)\s+warnings?\b", re.I
)
_LOCALHOST_PORT_RE = re.compile(
    r"\b(?:https?://)?(localhost|127\.0\.0\.1)(?::(\d{2,5}))?\b", re.I
)
_IPV4_RE = re.compile(r"\b(?<!/)((?:\d{1,3}\.){3}\d{1,3})\b")
_ENV_REF_RE = re.compile(
    r"(?:process\.env\.)([A-Z][A-Z0-9_]{2,})|\$\{([A-Z][A-Z0-9_]{2,})\}"
)
_SCREAMING_SNAKE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b")
_CAMEL_RE = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b")
_PATH_RE = re.compile(
    r"(?<![\w])/?(?:(?:\.{1,2}|~|home|Users|usr|opt|var|tmp)/)?"
    r"(?:src|lib|libs|app|apps|tests?|config|scripts?|packages?|plugins?|"
    r"tools|docs|bin|dist|build|agent|gateway|hermes_cli|retinue-web|"
    r"node_modules)/"
    r"[A-Za-z0-9_./+\-]+\.[A-Za-z0-9]{1,8}"
)
_UNIX_ABS_RE = re.compile(
    r"(?<![\w])(?:/home|/usr|/opt|/var|/tmp|/etc)/[A-Za-z0-9_./+\-]+"
)
_FILENAME_RE = re.compile(
    r"(?<![\w./])([A-Za-z][\w.-]*\.(?:ts|tsx|js|jsx|py|rb|go|rs|json|md|ya?ml|"
    r"toml|sh|css|html|sql|txt|lock|env))\b"
)
_VERSION_RE = re.compile(r"\b[vV](\d+)(?:\.(\d+)){1,3}(?:-[A-Za-z0-9.]+)?\b")
_NODE_LT_RE = re.compile(r"\bNode\.js\s*<\s*(\d+)\b")
_DURATION_RE = re.compile(
    r"(?<![\w])(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|min|mins|h|hr|hrs)\b", re.I
)
_SIZE_RE = re.compile(
    r"(?<![\w])(\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|KB|MB|GB|TB|B)\b", re.I
)
_PERCENT_RE = re.compile(r"(?<![\w])(\d+(?:\.\d+)?)\s*%")
_ARROW_RE = re.compile(r"\s*(?:→|⇒|->|=>)\s*")
_JSON_OBJECT_RE = re.compile(r"\{[^{}]{0,500}\}")
_STATUS_CHECK_RE = re.compile(
    r"\b(build|tests?|lint|ci|deploy)\s*:\s*[✅✔]\s*", re.I
)
_OPTIONAL_CHAIN_RE = re.compile(r"\?\.")
_NULLISH_RE = re.compile(r"\s*\?\?\s*")
_TEMPLATE_WRAP_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_BEARER_RE = re.compile(r"\bAuthorization:\s*Bearer\b", re.I)
_CONTENT_TYPE_JSON_RE = re.compile(r"\bContent-Type:\s*application/json\b", re.I)
_FETCH_CALL_RE = re.compile(r"\bfetch\(", re.I)
_REPEATED_PUNCT_RE = re.compile(r"([!?.,])\1{1,}")
_MASK_RE = re.compile(
    r"«redacted:[^»]+»|\*{3}|[A-Za-z0-9_-]{2,6}\.{3}[A-Za-z0-9_-]{2,6}"
)


def _stash_span(bank: _Bank, text: str, pattern: re.Pattern[str], transform: Callable[[re.Match[str]], str]) -> str:
    def repl(m: re.Match[str]) -> str:
        spoken = transform(m)
        if spoken is None:
            return m.group(0)
        if spoken == "":
            return " "
        return bank.stash(spoken)
    return pattern.sub(repl, text)


# ---------------------------------------------------------------------------
# Public stage-1 entry
# ---------------------------------------------------------------------------

def normalize_deterministic(text: str, config: HumanizeConfig | None = None) -> str:
    """Return a spoken script. Never raises."""
    cfg = config or HumanizeConfig()
    if not text:
        return ""
    try:
        return _normalize_deterministic(text, cfg)
    except Exception:
        try:
            from tools.tts_text_normalize import prepare_spoken_text

            return prepare_spoken_text(text, max_chars=None)
        except Exception:
            return str(text)


def _normalize_deterministic(text: str, cfg: HumanizeConfig) -> str:
    text = html.unescape(str(text))
    text = _strip_nonspoken(text)
    # Speak ${ENV} / process.env.ENV *names* before redaction, so a Bearer
    # header that holds a variable name is not treated as a secret value.
    text = re.sub(
        r"Authorization:\s*Bearer\s+\$\{?([A-Z][A-Z0-9_]*)\}?",
        lambda m: f"the Authorization Bearer header with the {split_ident(m.group(1))}",
        text,
        flags=re.I,
    )
    text = _TEMPLATE_WRAP_RE.sub(lambda m: split_ident(m.group(1)), text)
    text = _ENV_REF_RE.sub(
        lambda m: split_ident(m.group(1) or m.group(2)), text
    )
    text = _redact_for_speech(text)

    bank = _Bank()

    if cfg.code_blocks:
        text = _ITINERARY_FENCE_RE.sub(" ", text)
        text = _stash_span(
            bank, text, _CODE_FENCE_RE,
            lambda m: summarize_code_fence(m.group(1), m.group(2)),
        )
    else:
        text = _ITINERARY_FENCE_RE.sub(" ", text)
        text = _CODE_FENCE_RE.sub(" ", text)

    if cfg.urls:
        text = _stash_span(
            bank, text, _MD_LINK_RE,
            lambda m: m.group(1).strip()
            if m.group(1).strip() and "http" not in m.group(1).lower()
            else humanize_url(m.group(2)),
        )
        def _url_keep_punct(m: re.Match[str]) -> str:
            raw = m.group(0)
            trailing = ""
            while raw and raw[-1] in ".,);]:":
                trailing = raw[-1] + trailing
                raw = raw[:-1]
            return humanize_url(raw) + trailing

        text = _stash_span(bank, text, _URL_RE, _url_keep_punct)
    else:
        text = _MD_LINK_RE.sub(lambda m: m.group(1), text)
        text = _URL_RE.sub(" ", text)

    def _inline(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        return _humanize_inline(inner, cfg)

    text = _stash_span(bank, text, _INLINE_CODE_RE, _inline)

    # Status checkmarks before the generic emoji strip.
    text = _STATUS_CHECK_RE.sub(lambda m: f"{m.group(1)} successful. ", text)

    from tools.tts_text_normalize import (
        flatten_newlines_for_payload,
        normalize_symbols_for_tts,
        smooth_whitespace_for_tts,
        strip_markdown_for_tts,
    )

    text = strip_markdown_for_tts(text)
    text = re.sub(
        r"\b(Tests|Lint|Build):\s+(?:npm|pnpm|yarn|pytest|cargo|docker)[^\n→]*→\s*",
        lambda m: f"{m.group(1)}: ",
        text,
        flags=re.I,
    )
    text = _HTTP_STATUS_RE.sub(_http_status_spoken, text)
    text = _HTTP_METHOD_PATH_RE.sub(_api_path_spoken, text)
    text = _TEST_RATIO_RE.sub(_test_ratio_spoken, text)
    text = _ERR_WARN_RE.sub(_err_warn_spoken, text)
    text = _LOCALHOST_PORT_RE.sub(_localhost_spoken, text)
    text = _IPV4_RE.sub(_ipv4_spoken, text)
    text = _NODE_LT_RE.sub(
        lambda m: f"Node.js below {speak_int(int(m.group(1)))}", text
    )
    text = _DURATION_RE.sub(_duration_spoken, text)
    text = _SIZE_RE.sub(_size_spoken, text)
    text = _PERCENT_RE.sub(
        lambda m: f"{speak_number_token(m.group(1))} percent", text
    )
    text = _VERSION_RE.sub(_version_spoken, text)
    # Identifiers before filenames so process.env.FOO and ${FOO} are not
    # eaten as a ".env" / ".js" file.
    text = re.sub(r"\bNode\.js\b", lambda m: bank.stash("Node.js"), text)
    text = _TEMPLATE_WRAP_RE.sub(lambda m: split_ident(m.group(1)), text)
    text = _ENV_REF_RE.sub(
        lambda m: split_ident(m.group(1) or m.group(2)), text
    )
    text = _SCREAMING_SNAKE_RE.sub(lambda m: split_ident(m.group(0)), text)
    text = re.sub(
        r"\b(?:run\s+)?docker\s+compose\s+up[^\n]*--build\b",
        "rebuild and restart the services",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:run\s+)?docker\s+compose\s+up\b(?:\s+-\S+)*",
        "start the services",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(?:verify\s+)?curl\s+-X\s+GET\b", "verify", text, flags=re.I)
    text = re.sub(r"\bcurl\s+-X\s+POST\b", "a POST to", text, flags=re.I)
    text = re.sub(
        r"\bnpm\s+test(?:\s+--(?:\s+--[\w-]+)*)?",
        "npm test",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"--([A-Za-z][\w-]*)",
        lambda m: f" the {m.group(1).replace('-', ' ')} flag ",
        text,
    )
    text = re.sub(r"\s+--\s+", " ", text)
    text = _SNAKE_RE.sub(lambda m: split_ident(m.group(0)), text)
    text = _CAMEL_RE.sub(lambda m: split_ident(m.group(0)), text)
    text = _PATH_RE.sub(lambda m: bank.stash(humanize_path(m.group(0))), text)
    text = _UNIX_ABS_RE.sub(lambda m: bank.stash(humanize_path(m.group(0))), text)
    text = _FILENAME_RE.sub(lambda m: _filename_or_raw(bank, m.group(1)), text)
    text = _JSON_OBJECT_RE.sub(_json_inline_spoken, text)
    text = _BEARER_RE.sub("the Authorization Bearer header", text)
    text = _CONTENT_TYPE_JSON_RE.sub("JSON content type", text)
    text = _FETCH_CALL_RE.sub("the request to ", text)
    text = re.sub(r"the request to \s*[(\"]+", "the request to ", text)
    text = re.sub(r"(\"\s*\))", "", text)
    text = _OPTIONAL_CHAIN_RE.sub(" ", text)
    text = _NULLISH_RE.sub(" or ", text)
    text = _ARROW_RE.sub(" to ", text)
    text = _REPEATED_PUNCT_RE.sub(r"\1", text)
    # Protect compact "2m" measures from the shared cleaner turning `m` into metres.
    text = re.sub(
        r"(?<![\w])(\d+)m\b",
        lambda m: bank.stash(f"{speak_int(int(m.group(1)))} m"),
        text,
    )
    text = normalize_symbols_for_tts(text)
    # Smooth/flatten before restoring stashed spoken spans. The smoother
    # inserts a space after every period that precedes a letter.
    text = smooth_whitespace_for_tts(text)
    text = flatten_newlines_for_payload(text)
    text = bank.restore(text)
    text = _MASK_RE.sub(" a redacted secret ", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", " a redacted secret ", text)
    text = re.sub(r'(?<!\w)["\']([A-Za-z][\w-]*)["\'](?!\w)', r"\1", text)
    text = re.sub(r"\b(\d{1,3})\b", _speak_small_int, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(
        r"([.!?]\s+)([a-z])",
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text.strip()


def _strip_nonspoken(text: str) -> str:
    try:
        from tools.tts_text_normalize import strip_nonspoken_blocks

        text = strip_nonspoken_blocks(text)
    except Exception:
        pass
    text = _ITINERARY_FENCE_RE.sub(" ", text)
    return text


def _redact_for_speech(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        return text


def _humanize_inline(inner: str, cfg: HumanizeConfig) -> str:
    inner = inner.strip()
    if not inner:
        return ""
    if cfg.urls and re.match(r"https?://", inner):
        return humanize_url(inner)
    if inner.startswith("/") and ("{" in inner or inner.startswith("/api")):
        return _endpoint_from_path(inner)
    if "/" in inner and re.search(r"\.[A-Za-z0-9]{1,8}$", inner):
        return humanize_path(inner)
    if re.fullmatch(r"[A-Za-z][\w.-]*\.[A-Za-z0-9]{1,8}", inner):
        return _humanize_filename(inner)
    if re.fullmatch(r"(?:process\.env\.)?[A-Z][A-Z0-9_]+", inner) or inner.startswith("${"):
        return split_ident(inner)
    if re.fullmatch(r"[A-Za-z][\w-]*", inner) and ("_" in inner or re.search(r"[a-z][A-Z]", inner)):
        return split_ident(inner)
    if len(inner) < 48 and " " in inner and not inner.startswith("{") and "://" not in inner:
        # Short inline command.
        if inner.split()[0] in {"npm", "pnpm", "yarn", "git", "docker", "curl", "pytest", "cargo"}:
            return humanize_command(inner)
        return inner
    if _looks_like_json(inner):
        return _summarize_json(inner)
    if len(inner) > 80:
        return "the snippet on screen"
    return inner


def _http_status_spoken(m: re.Match[str]) -> str:
    code = int(m.group(1))
    spoken = speak_http_code(code)
    if code == 401:
        return f"an HTTP {spoken} error"
    if code == 403:
        return f"an HTTP {spoken} forbidden error"
    if code == 404:
        return f"an HTTP {spoken} not-found error"
    if 500 <= code <= 599:
        return f"a server error, HTTP {spoken}"
    if 400 <= code <= 499:
        return f"an HTTP {spoken} error"
    return f"HTTP {spoken}"


def _endpoint_from_path(path: str) -> str:
    # /api/v1/auth/refresh → the auth refresh endpoint
    # /api/v1/users/{id} → the user lookup endpoint
    cleaned = re.sub(r"\{[^}]+\}", "", path)
    parts = [p for p in cleaned.split("/") if p and p not in {"api", "v1", "v2", "v3"}]
    if not parts:
        return "the API endpoint"
    if parts[-1].lower() in {"id", ""}:
        parts = parts[:-1]
        if parts:
            return f"the {split_ident(parts[-1])} lookup endpoint"
    pretty = " ".join(split_ident(p) for p in parts[-2:]) if len(parts) >= 2 else split_ident(parts[-1])
    return f"the {pretty} endpoint"


def _api_path_spoken(m: re.Match[str]) -> str:
    method = m.group(1).upper()
    endpoint = _endpoint_from_path(m.group(2))
    body = endpoint[4:] if endpoint.startswith("the ") else endpoint
    if method == "DELETE":
        return f"the delete {body}"
    if method in {"POST", "PUT", "PATCH"}:
        return f"the {method} {body}"
    return endpoint


def _test_ratio_spoken(m: re.Match[str]) -> str:
    a, b = int(m.group(1)), int(m.group(2))
    if a == b and a > 0:
        return f"all {speak_int(a)} tests passed"
    return f"{speak_int(a)} of {speak_int(b)} tests passed"


def _err_warn_spoken(m: re.Match[str]) -> str:
    errors, warnings = int(m.group(1)), int(m.group(2))
    err = "no errors" if errors == 0 else f"{speak_int(errors)} errors"
    warn = "no warnings" if warnings == 0 else f"{speak_int(warnings)} warnings"
    return f"{err} and {warn}"


def _localhost_spoken(m: re.Match[str]) -> str:
    port = m.group(2)
    if port:
        return f"localhost on port {speak_int(int(port))}"
    return "localhost"


def _ipv4_spoken(m: re.Match[str]) -> str:
    ip = m.group(1)
    if ip in {"127.0.0.1", "0.0.0.0"}:
        return "localhost"
    parts = ip.split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return ip
    if len(nums) != 4 or any(n < 0 or n > 255 for n in nums):
        return ip
    return " dot ".join(speak_int(n) for n in nums)


def _duration_spoken(m: re.Match[str]) -> str:
    amount, unit = m.group(1), m.group(2).lower()
    word = _UNIT_WORDS.get(unit, unit)
    if amount == "1" and word.endswith("s"):
        word = word[:-1]
    return f"{speak_number_token(amount)} {word}"


def _size_spoken(m: re.Match[str]) -> str:
    amount, unit = m.group(1), m.group(2)
    word = _UNIT_WORDS.get(unit.lower(), unit)
    return f"{speak_number_token(amount)} {word}"


def _version_spoken(m: re.Match[str]) -> str:
    nums = re.findall(r"\d+", m.group(0))
    if not nums:
        return m.group(0)
    return "version " + " point ".join(speak_int(int(n)) for n in nums)


def _json_inline_spoken(m: re.Match[str]) -> str:
    blob = m.group(0)
    if ":" not in blob or '"' not in blob and "'" not in blob:
        return blob
    return _summarize_json(blob)
