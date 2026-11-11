"""Data-plane entry point. The ONLY code that reads user-supplied deck bytes.

Runs inside the sandbox (here: user+network namespace with rlimits; in
production: a Kata microVM). Contract: file paths + JSON in, files + JSON out.

    probe     <in.pptx> <index.json>
    execute   <in.pptx> <ops.json> <out.pptx> <report.json>
    validate  <in.pptx> <slides|all> <outdir|-> <report.json>
    autokit   <template.pptx> [--all] <draft_kit.json>
    marks     <template.pptx> <examples_dir> <targets.json> <report.json>
    selftest  <report.json>
"""
import json
import socket
import sys
import traceback


def _dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def main(argv):
    from deckops import zipguard
    cmd = argv[0]
    try:
        if cmd == "selftest":
            res = {}
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=3)
                res["network"] = "REACHABLE"
            except OSError as e:
                res["network"] = f"blocked ({e.__class__.__name__})"
            _dump(argv[1], res)
            return 0
        zipguard.check(argv[1])
        if cmd == "probe":
            from deckops.probe import probe
            _dump(argv[2], probe(argv[1]))
        elif cmd == "execute":
            from deckops.executor import ExecError, execute
            ops = json.load(open(argv[2]))
            try:
                _dump(argv[4], {"ok": True, **execute(argv[1], ops, argv[3])})
            except ExecError as e:
                _dump(argv[4], {"ok": False, "errors": e.errors})
                return 3
        elif cmd == "autokit":
            import os
            from deckops.autokit import draft_kit
            _dump(argv[-1], draft_kit(argv[1], os.path.basename(argv[1]), dedupe="--all" not in argv))
        elif cmd == "marks":
            # marks <deck.pptx> <examples_dir> <targets.json> <report.json>
            # targets.json: {"<slide number>": {"ids": [...], "item_ids": [...]}}
            import os
            from deckops.marks import mark
            from deckops.probe import probe
            index = probe(argv[1])
            out = {}
            for n, t in json.load(open(argv[3])).items():
                src = os.path.join(argv[2], f"slide-{int(n):02d}.png")
                out[n] = mark(src, os.path.join(argv[2], f"slide-{int(n):02d}-marks.png"),
                              index["slides"][int(n) - 1], index["slide_size"], t["ids"], t["item_ids"])
            _dump(argv[4], {"ok": True, "marked": out})
        elif cmd == "validate":
            from deckops.validate import validate
            slides = None if argv[2] == "all" else {int(x) for x in argv[2].split(",")}
            _dump(argv[4], validate(argv[1], slides, argv[3], do_render=argv[3] != "-"))
        else:
            raise SystemExit(f"unknown command {cmd}")
        return 0
    except zipguard.UnsafeArchive as e:
        _dump(argv[-1], {"ok": False, "rejected": str(e)})
        return 4
    except Exception:
        _dump(argv[-1], {"ok": False, "crash": traceback.format_exc(limit=5)})
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
