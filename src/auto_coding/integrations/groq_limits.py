"""Offline GPT-OSS sizing, separate from wire bytes and reported usage."""

import base64
from functools import lru_cache, partial
import gzip
import hashlib
from importlib.resources import files

import tiktoken

from ..reviews import ReviewConfig, ReviewError, ReviewPacket, ReviewPlan, encoded, plan_chunks


@lru_cache(maxsize=1)
def tokenizer() -> tiktoken.Encoding:
    data = gzip.decompress(files("auto_coding").joinpath("assets/o200k_base.tiktoken.gz").read_bytes())
    if hashlib.sha256(data).hexdigest() != "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d":
        raise ReviewError("Local tokenizer vocabulary integrity check failed")
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines())}
    # o200k_harmony uses these same ordinary-token ranks and segmentation.
    pattern = "|".join([
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
    ])
    return tiktoken.Encoding(name="review-o200k", pat_str=pattern, mergeable_ranks=ranks, special_tokens={})


def request_tokens(arguments: dict, *, headroom: int = 512) -> int:
    """Estimated input plus MAX output and generous provider framing margin.

    Not billed usage or an exact server chat-template measurement. All source
    text is ordinary text, including strings resembling special token markers.
    """
    if arguments["model"] != "openai/gpt-oss-120b":
        raise ReviewError("No verified local tokenizer for the selected review model")
    encode = tokenizer().encode_ordinary
    inputs = sum(len(encode(message["content"])) + len(encode(message["role"])) + 32
                 for message in arguments["messages"])
    inputs += len(encode(encoded(arguments["response_format"])))
    return inputs + arguments["max_completion_tokens"] + max(512, headroom)


def provider_plan(packet: ReviewPacket, config: ReviewConfig, limits: dict | None = None, *,
                  instructions: str | None = None) -> ReviewPlan:
    # Known smaller organization limits win. Never assume a paid tier.
    cap = 8000
    if limits and "x-ratelimit-limit-tokens" in limits:
        try:
            cap = min(cap, int(limits["x-ratelimit-limit-tokens"]))
        except (ValueError, TypeError):
            raise ReviewError("Provider token capacity is invalid") from None
    if cap <= 0:
        raise ReviewError("Provider token capacity is unavailable")
    return plan_chunks(packet, config, instructions=instructions,
                       token_counter=partial(request_tokens, headroom=config.headroom), token_limit=cap)
