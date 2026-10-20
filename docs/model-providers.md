# Model providers and media

This is a public-documentation inventory checked on **2026-10-20**, not a guarantee of account access, availability, or an exhaustive list of private resellers. It covers publicly documented GPT-6 Luna Decisions routes found in this review. Provider limits and wire formats can change; follow the linked primary sources before building another integration.

For the bot's supported secrets and routing order, use [provider setup](setup.md#optional-model-provider). `MODEL_PROVIDERS` in [`src/anti_fwd_spam/constants.py`](../src/anti_fwd_spam/constants.py) is the implementation authority. A platform in this inventory is not automatically a supported bot configuration.

## Built-in routes and evidence

The bot uses the existing Experiential and Vercel credentials for Luna Decisions, followed by the existing TypeSafe, OpenCode, and CommandCode routes for legacy Jev. Only configured credentials participate. No direct OpenAI, OpenRouter, or other reseller credential is added by this inventory.

Experiential's **text** route has been exercised with real responses. The other Luna routes and all image/video capabilities below are documentation findings, not live integration or semantic-accuracy results. Deterministic tests of request formatting and answer parsing do not establish that a remote account can call a model. Use [live model checks](jev-checks.md) for approved text fixtures; retain experiment results and model comparisons in the pull request rather than this reference.

## Public Luna transports

All paths below are HTTP `POST` endpoints, not chat-completion routes. The same model family does not imply the same JSON contract.

| Platform | Documented endpoint and model | Request and answer shape |
| --- | --- | --- |
| [OpenAI](https://developers.openai.com/api/docs/guides/decisions) | `https://api.openai.com/v1/decisions`; `gpt-6-luna` | Native Decisions: `input`, a `questions` array, and an `answers` array. A yes/no condition is a `predicate`, returning `probability`. |
| [Experiential](https://platform.experientiallabs.ai/models/gpt-6-luna-decisions) | `https://api.experientiallabs.ai/v1/decisions`; `gpt-6-luna-decisions` | Native Decisions shape. The platform documents at most 32 questions per request; gateway admission limits are distinct from OpenAI's own limits. |
| [Vercel AI Gateway](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-decisions) | `https://ai-gateway.vercel.sh/v1/decisions`; `openai/gpt-6-luna-decisions` | OpenAI-compatible Decisions shape, including `predicate` and an `answers` array. |
| [OpenRouter](https://openrouter.ai/openai/gpt-6-luna-decisions) | `https://openrouter.ai/api/alpha/decisions`; `openai/gpt-6-luna-decisions` | System One/Jev-style `state` and named `questions` object. A yes/no question is `noul`, not native `predicate`. Typed answers are keyed by question ID. |
| [LLM Gateway](https://llmgateway.io/blog/openai-decisions-api) | `https://api.llmgateway.io/v1/decisions`; `gpt-6-luna-decisions` | OpenAI-compatible Decisions shape. The documented examples use `input`, `predicate`/`choice`/`score`, and an `answers` array. |
| [RouterPlus](https://app.routerplus.com/docs/api-decisions) | `https://api.routerplus.com/v1/decisions`; `openai/gpt-6-luna-decisions` | System One `state`/`questions` and Jev-shaped answers. Provider ID `openai-decisions` maps to OpenAI's native `gpt-6-luna`; the gateway translates both directions. |
| [decisions-api.dev](https://decisions-api.dev/model/gpt-6-luna-decisions) | `https://decisions-api.dev/v1/systemone`; `openai/gpt-6-luna-decisions` | Independent OpenRouter-backed wrapper. System One `state`/`questions`; successful answers at `data.result.answers`, not top-level `answers`. |
| [decisionapi.net](https://decisionapi.net/decisions-api-luna-model) | `https://decisionapi.net/v1/systemone`; `openai/gpt-6-luna-decisions` | Independent OpenRouter-backed wrapper using `state`/`questions` and wrapped `data.result.answers`. The platform documents at most 8 questions and a 32 KiB request body. |
| [decisionsapi.pro](https://decisionsapi.pro/docs) | `https://decisionsapi.pro/api/v1/decisions`; `gpt-6-luna` | Independent service, not affiliated with OpenAI. Its own shape uses `context`, a named `questions` object, and `decisions` keyed by question ID. It also documents acceptance of OpenAI's request shape. |

An endpoint named `/decisions` is not sufficient evidence of compatibility. In particular, OpenRouter and RouterPlus expose System One-style questions on such a path, while decisionsapi.pro supports both its own format and an OpenAI-compatible format. An incompatible request or answer shape needs an adapter, not just a URL or model-ID change. Every additional bot route needs explicit configuration and request/parser tests; a public model listing is not verification of that integration.

## Image support by transport

These are upstream capabilities, **not bot features**. Text containing an image URL or a media-shaped JSON object is not proof that pixels reach the model.

| Platform | Documented Luna image input | Restrictions and evidence boundary |
| --- | --- | --- |
| OpenAI | Native `input_image` parts in message content. | The [create schema](https://developers.openai.com/api/reference/resources/decisions/methods/create) accepts base64 data URLs or publicly accessible HTTP(S) image URLs, up to 128 images. File IDs, files, audio, and item references are unsupported. |
| Experiential | `input_text` and inline base64 `input_image` parts in user messages. | The model page documents text and inline images. External image-URL support and a platform-specific image-count limit were not established by the reviewed page. |
| Vercel AI Gateway | Native `input_image` parts with base64 data URLs. | At most 128 images. External URLs and file IDs are explicitly unsupported, even though the native OpenAI schema accepts public image URLs. |
| OpenRouter | Top-level `state` array containing strings and `{"type":"image_url","image_url":{"url":"data:image/...;base64,..."}}` parts. | The [multimodal guide](https://openrouter.ai/docs/guides/community/multimodal-decisions) allows PNG, JPEG, and WebP data URLs, at most 128 images for Luna. Remote URLs are not fetched. Nested image objects, top-level `images`, and native `input_image` parts do not supply Luna images on this interface. |
| LLM Gateway | User messages containing `input_text` and `input_image`. | Image input is explicitly documented. External URL support, file-ID handling, and a gateway-specific image-count limit were not independently established from the reviewed announcement. |
| RouterPlus | No image forwarding on its Luna Decisions route. | Its documentation explicitly says structured state is serialized as text. An `image_url` object is read as JSON text, not as an image. OpenAI's native image capability does not carry through this gateway. |
| decisions-api.dev | Shared `images` array of `{url}` objects, or a `state` array of image parts, converted for OpenRouter. | Its [API docs](https://decisions-api.dev/docs) allow up to 4 PNG, JPEG, or WebP images: 4 MiB and 16 MP each, 8 MiB combined. Text/questions stay within 32 KiB; the complete image request may use 13 MiB. Public HTTPS downloads are restricted to configured hosts; base64 data URLs are accepted. These are the wrapper's bounds, not OpenRouter's. |
| decisionapi.net | Not independently verified for Luna through this wrapper. | Its Luna page establishes text/JSON decision access, not a verified image transport. Do not inherit image support or limits from OpenRouter or a similarly named site. |
| decisionsapi.pro | Optional `images`, using public HTTPS URLs or base64 data URLs. | Its [reference](https://decisionsapi.pro/docs) allows up to 4 images, 5 MB each, in PNG, JPEG, WebP, or GIF format. HTTPS images are downloaded by the service; redirects are not accepted. This is the service's documented behavior, not a live image test. |

The decisionsapi.pro reference says OpenAI accepts only inline images, while the reviewed OpenAI create schema also permits public HTTP(S) URLs. The native schema is the source for direct OpenAI support; a reseller's restrictions and claims must be evaluated separately.

## Video and audio boundaries

The reviewed Luna Decisions interfaces establish text and still-image evaluation, not native video or audio understanding. OpenAI's create schema does not offer a video input part and explicitly excludes audio and files. Uploading a video file, embedding its URL, or serializing a `video_url` object does not establish video understanding.

OpenRouter's multimodal guide identifies **Clef Omni**, not Luna, as the model that reads audio/video parts. Other Decisions models can see those parts as plain JSON text; successful HTTP delivery is not a vision or audio result. Clef Omni's support cannot be attributed to Luna, Clef, or Clef Flash.

A separate media implementation could sample video frames and submit them as still images on an image-capable transport. That would be frame-based analysis, not native video or audio understanding, and would require its own sampling policy, size limits, privacy review, error handling, and behavior tests.

## Bot media boundary

The bot submits bounded text context, selected profiles, and source-separated references. It does not fetch Telegram image/video/audio bytes, run OCR, sample frames, transcribe audio, or attach media to a model request. Media-presence metadata and captions are not pixel or audio ingestion.

Provider image support does not authorize forwarding Telegram file URLs. Those URLs can contain the bot token; they must not be sent to a model provider or written to logs. A future media feature needs an explicit authenticated download boundary and sanitized inline data, not automatic forwarding of token-bearing URLs. Existing moderation targets, invalid-answer handling, and retry policy remain defined by the [behavior contract](behavior.md#model-classification).
