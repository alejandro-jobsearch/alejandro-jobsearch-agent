"""Phase 0 smoke test: plain completion + JSON output against the configured endpoint."""
import json
import sys
import time

from jobagent.llm import client, model


def main() -> int:
    c, m = client(), model()
    print(f"model={m}")

    t = time.perf_counter()
    r = c.chat.completions.create(
        model=m,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        max_tokens=20,
    )
    print(f"plain: {r.choices[0].message.content!r} ({time.perf_counter() - t:.1f}s, usage={r.usage})")

    t = time.perf_counter()
    try:
        r = c.chat.completions.create(
            model=m,
            messages=[
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": 'Score 0-100 how well "Head of AI, Lima" fits a cloud & AI architect. '
                                            'Schema: {"score": int, "reason": str}'},
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
        )
        data = json.loads(r.choices[0].message.content)
        print(f"json_mode: ok {data} ({time.perf_counter() - t:.1f}s)")
    except Exception as e:  # json mode support varies across providers
        print(f"json_mode: FAILED {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
