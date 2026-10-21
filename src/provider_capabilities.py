"""Which LLM settings each provider actually honours.

Every backend is reached through the same OpenAI-compatible
``/chat/completions`` endpoint, so the request body has the same shape for
all of them - but the backends do not agree on which parameters they
implement.  OpenAI and Mistral reject unknown keys outright, llama.cpp-derived
servers accept a handful of extra samplers under their own names, and
several services simply ignore what they do not know.

This module holds that knowledge in one place so the settings dialog can
flag the settings the selected provider will not honour, instead of leaving
the user wondering why a slider does nothing.

The data comes from each vendor's own documentation or source for its
OpenAI-compatible endpoint; where a vendor does not document a parameter, it
is treated as unsupported.  Only the samplers that differ from the OpenAI
set are listed - a setting that is absent from a provider's entry is sent
under its own name and is expected to work.
"""

from __future__ import annotations

#: The OpenAI-standard sampling parameters.  Nearly every provider
#: implements these; the exceptions are listed per provider below.
STANDARD_SETTINGS: tuple[str, ...] = (
    'top_p', 'frequency_penalty', 'presence_penalty', 'seed', 'stop',
)

#: The samplers only the llama.cpp-family servers implement.  Each server
#: spells them slightly differently, so they are mapped per provider below.
_LLAMA_CPP_ONLY: tuple[str, ...] = (
    'mirostat', 'mirostat_tau', 'mirostat_eta',
    'dry_multiplier', 'dry_base', 'dry_allowed_length', 'dry_penalty_range',
    'xtc_threshold', 'xtc_probability',
    'typical_p', 'dynatemp_range', 'dynatemp_exponent', 'smoothing_factor',
    'min_tokens_to_keep',
)

#: The samplers that go beyond the OpenAI set.
EXTENDED_SETTINGS: tuple[str, ...] = (
    'top_k', 'min_p', 'repeat_penalty',
) + _LLAMA_CPP_ONLY

#: Every setting that is only sent when it differs from its neutral value.
#: Everything else either always goes out (``model``, ``messages``,
#: ``temperature``, ``max_tokens``, ``stream``) or never leaves the app
#: (context size, retries, the chat template).
OPTIONAL_SETTINGS: tuple[str, ...] = STANDARD_SETTINGS + EXTENDED_SETTINGS


def _entry(unsupported: tuple[str, ...] = (), **renames: str | None) -> dict[str, str | None]:
    """Build a provider's capability entry.

    *unsupported* names the settings the provider does not implement; the
    rest of the optional settings are sent under their own name.  A rename
    maps a setting to the key the provider expects, and ``None`` there marks
    it as unsupported.
    """
    table: dict[str, str | None] = dict.fromkeys(unsupported, None)
    table.update(renames)
    return table


#: provider -> {setting -> the request-body key it uses}.
#:
#: ``None`` means the provider does not implement the setting: the value is
#: left out of the request and the settings dialog warns about it.  A
#: different key name means the setting *is* supported, but under another
#: spelling - the repetition penalty is ``repeat_penalty`` for the
#: llama.cpp family and ``repetition_penalty`` for everyone else, and
#: Mistral calls the seed ``random_seed``.
PROVIDER_SUPPORT: dict[str, dict[str, str | None]] = {
    'LM Studio': _entry(
        # Its OpenAI-compatible endpoint documents top_k and repeat_penalty,
        # but not min_p or any of the llama.cpp-only samplers.
        unsupported=('min_p', *_LLAMA_CPP_ONLY),
    ),
    'Ollama': _entry(
        # Its OpenAI-compatible shim exposes neither the llama.cpp samplers
        # nor min_p.
        unsupported=('top_k', 'min_p', 'repeat_penalty', *_LLAMA_CPP_ONLY),
    ),
    'text-generation-webui': _entry(
        # A llama.cpp-family server with SillyTavern-style parameter names.
        unsupported=('dynatemp_range', 'dry_penalty_range', 'min_tokens_to_keep'),
        repeat_penalty='repetition_penalty',
        mirostat='mirostat_mode',
    ),
    'KoboldCpp': _entry(
        # Accepts repeat_penalty, repetition_penalty and rep_pen alike.
        unsupported=('min_tokens_to_keep',),
        mirostat='mirostat_mode',
        dry_penalty_range='dry_penalty_last_n',
        typical_p='typical',
    ),
    'llama.cpp': _entry(
        # The reference implementation: every sampler, under the names the
        # server's own schema uses.
        dry_penalty_range='dry_penalty_last_n',
        min_tokens_to_keep='min_keep',
    ),
    'vLLM': _entry(
        unsupported=_LLAMA_CPP_ONLY,
        repeat_penalty='repetition_penalty',
    ),
    'Jan': _entry(
        unsupported=_LLAMA_CPP_ONLY,
        seed=None,
        repeat_penalty='repetition_penalty',
    ),
    'GPT4All': _entry(
        # Rejects these with a 400 rather than ignoring them; the repeat
        # penalty and top_k are model settings, not request parameters.
        unsupported=('top_k', 'repeat_penalty', *_LLAMA_CPP_ONLY),
        frequency_penalty=None, presence_penalty=None, seed=None, stop=None,
    ),
    'LocalAI': _entry(
        # A llama.cpp wrapper, but its request struct only carries a few of
        # the samplers - the rest live in the model config.
        unsupported=(
            'mirostat', 'mirostat_tau', 'mirostat_eta',
            'dry_multiplier', 'dry_base', 'dry_allowed_length',
            'dry_penalty_range', 'xtc_threshold', 'xtc_probability',
            'dynatemp_range', 'dynatemp_exponent', 'smoothing_factor',
        ),
        min_tokens_to_keep='n_keep',
    ),
    'OpenRouter': _entry(
        unsupported=_LLAMA_CPP_ONLY,
        repeat_penalty='repetition_penalty',
    ),
    'OpenAI': _entry(
        unsupported=EXTENDED_SETTINGS,
    ),
    'Gemini': _entry(
        unsupported=EXTENDED_SETTINGS,
    ),
    'Mistral': _entry(
        unsupported=('min_p', *_LLAMA_CPP_ONLY),
        seed='random_seed',
        repeat_penalty='repetition_penalty',
    ),
    'DeepSeek': _entry(
        # The penalties are documented but deprecated: "it will not take
        # effect if you pass it to the API".
        unsupported=EXTENDED_SETTINGS,
        frequency_penalty=None, presence_penalty=None,
    ),
    'Groq': _entry(
        unsupported=EXTENDED_SETTINGS,
    ),
    'xAI Grok': _entry(
        # top_k and min_p only exist on the Responses API, not on chat
        # completions.
        unsupported=EXTENDED_SETTINGS,
    ),
}

#: Providers that answer an unknown request parameter with an error instead
#: of ignoring it.  The settings dialog words its warning accordingly: the
#: request will fail rather than just quietly do nothing.
PROVIDER_REJECTS_UNKNOWN: frozenset[str] = frozenset({
    'OpenAI', 'Mistral', 'GPT4All',
})


def request_key(provider: str, setting: str) -> str | None:
    """The request-body key *setting* is sent under for *provider*.

    Returns ``None`` when the provider does not implement the setting, in
    which case the value must be left out of the request entirely.  An
    unknown provider (Custom) keeps the setting's own name, which is what
    most OpenAI-compatible servers expect.
    """
    return PROVIDER_SUPPORT.get(provider, {}).get(setting, setting)


def unsupported_settings(provider: str) -> set[str]:
    """The optional settings *provider* does not honour."""
    return {
        setting
        for setting in OPTIONAL_SETTINGS
        if request_key(provider, setting) is None
    }


def rejects_unknown_parameters(provider: str) -> bool:
    """True when *provider* errors out on a parameter it does not know."""
    return provider in PROVIDER_REJECTS_UNKNOWN
