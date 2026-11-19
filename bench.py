#!/usr/bin/env python3
"""Benchmark for Open LLM Desktop (EK Light LLM): how fast Gemma runs on this machine.

Measures, with the LiteRT-LM runtime's own counters:
  - model load time, and reload time (waking up after the idle unload)
  - time to first token, prefill and decode speed (tokens/second) for a short chat,
    a ~1.5k-token document question and a ~4k-token long-context question
  - memory: after loading, peak while answering, and after unloading (what an idle app keeps)

Run it with the app closed, so they don't compete for the CPU:

    .venv/bin/python bench.py                    # prints the results
    .venv/bin/python bench.py --update-readme    # also saves them and updates README.md
    .venv/bin/python bench.py --backend gpu --runs 5 --media

Results are kept in bench/results.json (one row per machine + backend + threads), and the
README table between the BENCH markers is rebuilt from that file, so runs on several
machines add up to one table.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import gc
import json
import os
import platform
import re
import statistics
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
RESULTS = APP_DIR / "bench" / "results.json"
README = APP_DIR / "README.md"
START, END = "<!-- BENCH:START -->", "<!-- BENCH:END -->"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ek-light-llm" / "config.json"
MAX_OUTPUT = 256

# A plain made-up document: its content doesn't matter for speed, only its length.
_DOC_SENTENCES = [
    "The warehouse in Section {n} stores cement, steel rods and roofing sheets for the northern region.",
    "Orders placed before noon in Section {n} are delivered the same day by the local fleet.",
    "Section {n} lists the safety rules: helmets on site, no loading after dark, two people per lift.",
    "Invoices for Section {n} are due within thirty days, with a two percent discount for early payment.",
    "Staff in Section {n} record stock counts every Friday and report shortages to the regional manager.",
]


def _document(engine, tokens: int) -> str:
    parts, n = [], 1
    while True:
        parts.append(" ".join(s.format(n=n) for s in _DOC_SENTENCES))
        n += 1
        text = "\n\n".join(parts)
        if n % 5 == 0 and len(engine.tokenize(text)) >= tokens:
            return text


SCENARIOS = [
    # name, prompt tokens (document size), question
    ("Short chat", 0, "In two sentences, why do people run language models on their own computer?"),
    ("Document Q&A (~1.5k tokens)", 1500, "Using the document above, summarise the delivery and payment rules."),
    ("Long context (~4k tokens)", 4000, "Using the document above, list the safety rules in three bullet points."),
]


# ---------------- system info ----------------
def _rss_mb(field: str = "VmRSS") -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _reset_peak() -> None:
    try:
        Path("/proc/self/clear_refs").write_text("5")  # resets VmHWM (peak RSS) for this process
    except OSError:
        pass


def _trim() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass


def system_info(backend: str, threads: int | None) -> dict:
    cpu = platform.processor() or ""
    try:
        m = re.search(r"^model name\s*:\s*(.+)$", Path("/proc/cpuinfo").read_text(), re.M)
        cpu = m.group(1).strip() if m else cpu
    except OSError:
        pass
    ram = 0.0
    try:
        ram = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) / 1024 / 1024
    except (OSError, AttributeError):
        pass
    osname = platform.platform()
    try:
        osname = re.search(r'^PRETTY_NAME="?([^"\n]+)', Path("/etc/os-release").read_text(), re.M).group(1)
    except (OSError, AttributeError):
        pass
    try:
        from importlib.metadata import version
        rt = version("litert-lm-api")
    except Exception:  # noqa: BLE001
        rt = "?"
    return {"cpu": re.sub(r"\s+", " ", re.sub(r"\((R|TM)\)|CPU|Processor|\d+-Core", "", cpu)).strip(),
            "cores": os.cpu_count(), "ram_gb": round(ram, 1), "os": osname, "python": platform.python_version(),
            "runtime": f"litert-lm-api {rt}", "backend": backend, "threads": threads or "auto"}


# ---------------- measuring ----------------
def _model_path(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    try:
        p = json.loads(CONFIG.read_text()).get("model_path")
        if p and Path(p).is_file():
            return Path(p)
    except Exception:  # noqa: BLE001
        pass
    for p in (APP_DIR / "models" / "gemma-4-E2B-it.litertlm",
              Path.home() / ".local/share/ek-light-llm/models/gemma-4-E2B-it.litertlm"):
        if p.is_file():
            return p
    sys.exit("Model not found. Download it in the app first, or pass --model /path/to/model.litertlm")


def _load(lm, path: Path, backend: str, threads: int | None, ctx: int, cache: Path, media: bool):
    main = lm.Backend.GPU() if backend == "gpu" else lm.Backend.CPU(thread_count=threads)
    kw = dict(backend=main, max_num_tokens=ctx, cache_dir=str(cache), enable_benchmark=True)
    if media:
        kw.update(vision_backend=lm.Backend.CPU(), audio_backend=lm.Backend.CPU(), max_num_images=4)
    t0 = time.perf_counter()
    engine = lm.Engine(str(path), **kw)
    return engine, time.perf_counter() - t0


def _ask(lm, engine, prompt: str) -> dict:
    conv = engine.create_conversation(
        system_message="You are a helpful assistant.",
        sampler_config=lm.SamplerConfig(top_k=1, top_p=1.0, temperature=0.0),  # same answer every run
        max_output_tokens=MAX_OUTPUT)
    t0 = time.perf_counter()
    first = None
    text = []
    try:
        for chunk in conv.send_message_async({"role": "user", "content": [{"type": "text", "text": prompt}]}):
            for item in chunk.get("content", []) or []:
                if item.get("type") == "text" and item.get("text"):
                    if first is None:
                        first = time.perf_counter() - t0
                    text.append(item["text"])
        total = time.perf_counter() - t0
        try:
            info = conv.get_benchmark_info()
            out = {"ttft": info.time_to_first_token_in_second,
                   "prefill_tokens": info.last_prefill_token_count, "prefill_tps": info.last_prefill_tokens_per_second,
                   "decode_tokens": info.last_decode_token_count, "decode_tps": info.last_decode_tokens_per_second}
        except Exception:  # noqa: BLE001  (older runtime: fall back to wall-clock timing)
            n_out = len(engine.tokenize("".join(text)))
            n_in = len(engine.tokenize(prompt))
            out = {"ttft": first or total, "prefill_tokens": n_in, "prefill_tps": n_in / max(first or total, 1e-6),
                   "decode_tokens": n_out, "decode_tps": n_out / max(total - (first or 0), 1e-6)}
        out["total"] = total
        return out
    finally:
        conv.close()


def run(args) -> dict:
    import litert_lm as lm

    lm.set_min_log_severity(lm.LogSeverity.ERROR)
    path = _model_path(args.model)
    cache = Path(args.cache).expanduser() if args.cache else APP_DIR / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    info = system_info(args.backend, args.threads)
    info.update(model=path.name, model_gb=round(path.stat().st_size / 1e9, 2), context=args.context,
                runs=args.runs, date=dt.date.today().isoformat())
    print(f"Model {path.name} on {info['cpu']} ({info['cores']} threads, {info['ram_gb']} GB RAM), "
          f"{args.backend.upper()}, {args.runs} runs per test\n", flush=True)

    base_rss = _rss_mb()
    _reset_peak()
    engine, load = _load(lm, path, args.backend, args.threads, args.context, cache, media=False)
    loaded_rss = _rss_mb() - base_rss
    print(f"  load            {load:6.2f} s   memory {loaded_rss:6.0f} MB", flush=True)

    scenarios = []
    doc_cache: dict[int, str] = {}
    for name, doc_tokens, question in SCENARIOS:
        if doc_tokens + MAX_OUTPUT + 200 > args.context:
            print(f"  {name}: skipped (needs a context over {args.context})")
            continue
        if doc_tokens:
            doc_cache.setdefault(doc_tokens, _document(engine, doc_tokens))
            prompt = f"<document>\n{doc_cache[doc_tokens]}\n</document>\n\n{question}"
        else:
            prompt = question
        _ask(lm, engine, prompt)  # warm-up, not counted
        runs = [_ask(lm, engine, prompt) for _ in range(args.runs)]
        med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
        med["name"] = name
        scenarios.append(med)
        print(f"  {name:30s} first token {med['ttft']:5.2f} s · prefill {med['prefill_tps']:7.1f} tok/s "
              f"({med['prefill_tokens']:.0f} tok) · decode {med['decode_tps']:5.1f} tok/s", flush=True)
    peak_rss = _rss_mb("VmHWM") - base_rss

    engine.close()
    del engine
    _trim()
    idle_rss = max(0.0, _rss_mb() - base_rss)
    _reset_peak()
    engine, reload = _load(lm, path, args.backend, args.threads, args.context, cache, media=False)
    print(f"  reload (wake)   {reload:6.2f} s   peak {peak_rss:6.0f} MB   after unload {idle_rss:4.0f} MB", flush=True)
    engine.close()
    del engine
    _trim()

    media = None
    if args.media:
        before = _rss_mb()
        engine, mload = _load(lm, path, args.backend, args.threads, args.context, cache, media=True)
        media = {"load": mload, "rss": _rss_mb() - before}
        print(f"  load with image+audio  {mload:6.2f} s   memory {media['rss']:6.0f} MB", flush=True)
        engine.close()
        del engine
        _trim()

    return {"system": info, "load": load, "reload": reload, "rss_loaded": loaded_rss, "rss_peak": peak_rss,
            "rss_idle": idle_rss, "media": media, "scenarios": scenarios}


# ---------------- README ----------------
def _key(r: dict) -> tuple:
    s = r["system"]
    return (s["cpu"], s["ram_gb"], s["backend"], str(s["threads"]), s["model"], s["context"])


def save(result: dict) -> list[dict]:
    rows = []
    if RESULTS.is_file():
        try:
            rows = json.loads(RESULTS.read_text())
        except ValueError:
            rows = []
    rows = [r for r in rows if _key(r) != _key(result)] + [result]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(rows, indent=2) + "\n")
    return rows


def _scen(r: dict, prefix: str) -> dict | None:
    return next((s for s in r["scenarios"] if s["name"].startswith(prefix)), None)


def markdown(rows: list[dict]) -> str:
    def f(v, fmt):
        return "–" if v is None else format(v, fmt)

    out = ["| Machine | Backend | Load | First token (short) | Decode | Prefill (1.5k doc) | First token (1.5k doc) | RAM loaded | RAM idle |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        s = r["system"]
        short, doc = _scen(r, "Short"), _scen(r, "Document")
        out.append(
            f"| {s['cpu']}, {s['ram_gb']:g} GB | {s['backend'].upper()} ({s['threads']} threads) | {r['load']:.1f} s "
            f"| {f(short and short['ttft'], '.2f')} s | {f(short and short['decode_tps'], '.1f')} tok/s "
            f"| {f(doc and doc['prefill_tps'], '.0f')} tok/s | {f(doc and doc['ttft'], '.1f')} s "
            f"| {r['rss_loaded'] / 1024:.2f} GB | {r['rss_idle']:.0f} MB |")
    last = rows[-1]["system"]
    out += ["", f"<sub>Model {last['model']} ({last['model_gb']} GB), context {last['context']} tokens, "
                f"greedy decoding, up to {MAX_OUTPUT} output tokens, median of {last['runs']} runs after a warm-up. "
                f"Measured with `bench.py` using LiteRT-LM's own counters ({last['runtime']}, {last['os']}). "
                "\"RAM idle\" is what stays in use after the model unloads.</sub>"]

    detail = ["", "<details><summary>All results</summary>", ""]
    for r in rows:
        s = r["system"]
        detail += [f"**{s['cpu']}** · {s['cores']} threads · {s['ram_gb']:g} GB · {s['backend'].upper()} · {s['date']}", "",
                   "| Test | Prompt tokens | First token | Prefill | Output tokens | Decode |", "|---|---|---|---|---|---|"]
        for sc in r["scenarios"]:
            detail.append(f"| {sc['name']} | {sc['prefill_tokens']:.0f} | {sc['ttft']:.2f} s | {sc['prefill_tps']:.0f} tok/s "
                          f"| {sc['decode_tokens']:.0f} | {sc['decode_tps']:.1f} tok/s |")
        extra = f"Load {r['load']:.1f} s · reload after idle {r['reload']:.1f} s · peak RAM {r['rss_peak'] / 1024:.2f} GB"
        if r.get("media"):
            extra += f" · load with image+audio {r['media']['load']:.1f} s ({r['media']['rss'] / 1024:.2f} GB)"
        detail += ["", extra, ""]
    detail.append("</details>")
    return "\n".join(out + detail)


def update_readme(rows: list[dict]) -> None:
    text = README.read_text()
    if START not in text or END not in text:
        sys.exit(f"README.md has no {START} … {END} section.")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    README.write_text(f"{head}{START}\n{markdown(rows)}\n{END}{tail}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", help="path to a .litertlm model (default: the app's model)")
    ap.add_argument("--backend", choices=["cpu", "gpu"], default="cpu")
    ap.add_argument("--threads", type=int, default=None, help="CPU threads (default: auto)")
    ap.add_argument("--context", type=int, default=8192, help="context window in tokens (default 8192, as in the app)")
    ap.add_argument("--runs", type=int, default=3, help="measured runs per test (median is reported)")
    ap.add_argument("--media", action="store_true", help="also time loading with image and audio support")
    ap.add_argument("--cache", help="compiled-model cache folder (default: the app's .cache)")
    ap.add_argument("--update-readme", action="store_true", help="save to bench/results.json and update README.md")
    ap.add_argument("--readme-only", action="store_true", help="rebuild the README table from bench/results.json")
    args = ap.parse_args()

    if args.readme_only:
        update_readme(json.loads(RESULTS.read_text()))
        print("README.md updated.")
        return
    result = run(args)
    print("\n" + markdown([result]))
    if args.update_readme:
        update_readme(save(result))
        print(f"\nSaved to {RESULTS.relative_to(APP_DIR)} and updated README.md")


if __name__ == "__main__":
    main()
