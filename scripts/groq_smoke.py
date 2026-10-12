"""Explicitly opt-in live connectivity check, outside pytest discovery.

Does not review code, invent test results, or change task state.
"""

from __future__ import annotations

import argparse
import json
import os

import groq


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true", help="allow ONE real Groq API request")
    args = parser.parse_args(argv)
    if not args.allow_live or not os.environ.get("GROQ_API_KEY"):
        print("Refused: --allow-live and GROQ_API_KEY are both required.")
        return 1
    try:
        with groq.Groq(timeout=30.0, max_retries=0, base_url="https://api.groq.com") as client:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b", reasoning_effort="medium",
                messages=[{"role": "user", "content": 'Return JSON {"ok":true}. This is a connectivity check only.'}],
                response_format={"type": "json_schema", "json_schema": {
                    "name": "connectivity", "strict": True, "schema": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                    },
                }}, max_completion_tokens=512, include_reasoning=False,
            )
        if (completion.model != "openai/gpt-oss-120b" or len(completion.choices) != 1
                or completion.choices[0].finish_reason != "stop"
                or json.loads(completion.choices[0].message.content or "") != {"ok": True}):
            print("Smoke check failed: unexpected or incomplete response.")
            return 1
    except (groq.GroqError, ValueError, TypeError):
        print("Smoke check failed; inspect authentication, network and quota privately.")
        return 1
    print("Groq connectivity check passed (one live request; not a code approval).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
