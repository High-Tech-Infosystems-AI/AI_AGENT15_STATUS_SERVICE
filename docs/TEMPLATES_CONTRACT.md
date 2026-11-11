# WhatsApp Templates API contract

The template editor mirrors BroadNet's "Templates" product (WhatsApp section). This file is the contract the UI builds against.

- **Base path:** `/status/whatsapp` (status service).
- **Auth:** the HRMIS bearer token, as for every other status-service route.
- **Permissions:**
  - `whatsapp_templates.view` for list, get, options, validate and preview.
  - `whatsapp_templates.manage` for create, update, submit, delete, media upload, new-version and sync.
  - `mark-approved` is admin-tier only.
  - Admin and super-admin tiers always pass.
- **Organisation:** taken from the token. Super admins pass `organization_id` in the query string (or in the body on create).
- **Accounts:** a template belongs to one WhatsApp account (`account_id`). See §3.3 for how the account is chosen.

Code: `app/whatsapp_layer/api/templates_api.py`, `templates_service.py` and `template_rules.py` (rules and limits). Provider mapping is in `providers/omnichannel.py`.

---

## 1. Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/templates/options` | Enums, limits, button caps and availability, languages |
| GET | `/templates` | List with filters (§3.1) |
| GET | `/templates/{id}` | One template (§2.2) |
| POST | `/templates` | Create a draft (§3.3) |
| PUT | `/templates/{id}` | Replace a draft or rejected template |
| POST | `/templates/validate?for_submit=false` | Check an unsaved payload: `{valid, errors, warnings}` |
| POST | `/templates/preview` | Preview an unsaved payload (body = template payload + `values`) |
| POST | `/templates/{id}/preview` | Preview a saved template (body `{values?}` optional) |
| POST | `/templates/media` | Upload a header or carousel file (multipart) |
| POST | `/templates/{id}/submit` | Send to BroadNet / Meta for approval, which sets the status to `pending` |
| DELETE | `/templates/{id}` | Delete (204) |
| POST | `/templates/{id}/new-version` | Copy into a new draft `<base>_vN` (existing) |
| POST | `/templates/sync` | Import and refresh statuses from the provider (existing) |
| POST | `/templates/{id}/mark-approved` | Manual approval override, admin only (existing) |
| GET | `/variables` | Clawback variable catalogue (existing) |

---

## 2. Template object

### 2.1 Request payload (create, update, validate, preview)

Every field except `name` is optional. Enums are case-insensitive; the server upper-cases them.

```json
{
  "name": "new_collection_offer",
  "language": "en_US",
  "category": "MARKETING",
  "template_type": "DEFAULT",
  "variable_type": "NUMBER",
  "purpose": "generic",
  "header_format": "TEXT",
  "header_text": "New Arrival {{1}}",
  "header_sample_values": {"1": "Shoes"},
  "header_media": null,
  "body_text": "Hi {{1}}, your order {{2}} is ready.",
  "sample_values": {"1": "Abhishek", "2": "4679"},
  "footer_text": "Reply STOP to unsubscribe",
  "buttons": [
    {"type": "URL", "label": "Shop now", "url": "https://shop.hti.in/o/{{1}}", "url_example": "A123"},
    {"type": "QUICK_REPLY", "label": "View in app"}
  ],
  "limited_time_offer": {"text": "Expire soon", "expires_at": "2026-09-30", "has_expiration": true},
  "carousel_cards": null,
  "auth_config": null,
  "custom_validity_period": false,
  "validity_period_seconds": null,
  "account_id": 3
}
```

| Field | Notes |
|---|---|
| `name` | Must match `^[a-z][a-z0-9_]{0,511}$`. Unique per account and language. |
| `language` | Meta code, e.g. `en_US`, `en`, `hi`. Default `en_US`. |
| `category` | `MARKETING`, `UTILITY` or `AUTHENTICATION`. Default `UTILITY`. |
| `template_type` | The sub-type (§4.1). Omitted means `DEFAULT`, or `OTP` for Authentication. |
| `variable_type` | `NUMBER` (`{{1}}`) or `NAME` (`{{customer_name}}`). Omitted means it is worked out from the placeholders; the UI should always send it. One style applies to the header, body, URL and card bodies. |
| `purpose` | Clawback routing. Keep `generic` for editor templates. Other purposes require `NAME` and only allow catalogue variables (`GET /variables`). |
| `header_format` | `NONE`, `TEXT`, `IMAGE`, `VIDEO`, `DOCUMENT`, `LOCATION` or `CAROUSEL` (BroadNet's "Media Sample"). |
| `header_text` | `TEXT` only; dropped for other formats. At most 60 characters and one variable. In Number mode the variable must be `{{1}}`. |
| `header_sample_values` | Samples for header variables, `{key: value}`. Falls back to `sample_values` for the same key. |
| `header_media` | `IMAGE`, `VIDEO` and `DOCUMENT` only. Shape is a media object (§2.3). |
| `body_text` | Required, at most 1024 characters. Named variables count as `{{n}}`. Must not start or end with a variable, and two variables must not be adjacent. Ignored for `AUTHENTICATION`. |
| `sample_values` | Body samples, `{key: value}`. In Number mode the keys are `"1"`, `"2"` and so on. |
| `footer_text` | At most 60 characters, no variables. |
| `buttons` | See §4.3. |
| `limited_time_offer` | Marketing Default only. `null` means off; `{"enabled": false, ...}` is also treated as off. |
| `carousel_cards` | `CAROUSEL` only (§4.5). |
| `auth_config` | `AUTHENTICATION` only (§4.6). |
| `custom_validity_period`, `validity_period_seconds` | Utility only. When the toggle is on, seconds must be 30–43200. |
| `account_id` | Create only (can also be passed as `?account_id=`). Ignored on update. |

Sections the chosen category or header does not use are dropped silently, not rejected:

- Header text is dropped unless the header is `TEXT`.
- Media is dropped unless the header is `IMAGE`, `VIDEO` or `DOCUMENT`.
- Cards are dropped unless the header is `CAROUSEL`.
- For Authentication, the header, footer, body, buttons, LTO and cards are all dropped.

### 2.2 Response (create, update, get, submit, and each list item)

```json
{
  "id": 41, "organization_id": 12, "account_id": 3,
  "name": "new_collection_offer", "language": "en_US", "language_label": "English (US)",
  "category": "MARKETING", "template_type": "DEFAULT", "template_type_label": "DEFAULT",
  "purpose": "generic", "variable_type": "NUMBER",
  "header_format": "TEXT", "header_type": "TEXT", "header_text": "New Arrival {{1}}",
  "header_sample_values": {"1": "Shoes"}, "header_media": null,
  "body_text": "Hi {{1}}, your order {{2}} is ready.",
  "body_text_positional": "Hi {{1}}, your order {{2}} is ready.",
  "footer_text": "Reply STOP to unsubscribe",
  "buttons": [{"type": "URL", "label": "Shop now", "url": "https://shop.hti.in/o/{{1}}", "url_example": "A123"}],
  "button_types": ["URL"],
  "button_summary": ["Url: https://shop.hti.in/o/{{1}}"],
  "variables": ["1", "2"],
  "sample_values": {"1": "Abhishek", "2": "4679"},
  "limited_time_offer": null, "carousel_cards": null, "auth_config": null,
  "custom_validity_period": false, "validity_period_seconds": null, "time_to_live": 0,
  "provider_template_id": null,
  "approval_status": "draft", "status": "DRAFT",
  "rejection_reason": null, "previous_category": null, "quality_score": null,
  "last_synced_at": null, "submitted_at": null,
  "is_active": true, "version": 1, "in_use_by_rules": 0,
  "warnings": [],
  "created_at": "2026-11-11T10:00:00Z", "updated_at": "2026-11-11T10:00:00Z"
}
```

How the fields map to BroadNet's list columns:

| BroadNet column | Field |
|---|---|
| Name | `name` |
| Language | `language_label` |
| Category | `category` |
| Status | `status`: `DRAFT`, `PENDING`, `APPROVED`, `REJECTED`, `PAUSED` or `DISABLED`. The lowercase `approval_status` is also returned. |
| Previous Category | `previous_category` (`null` is shown as `-`) |
| (Quality) | `quality_score`: `GREEN`, `YELLOW`, `RED`, `UNKNOWN` or `null` (Meta's quality webhook; column added by `migrations/v32_whatsapp_template_quality.sql`, run by hand) |
| Template type | `template_type_label`: `DEFAULT`, `UTILITY DEFAULT`, `CATALOGUE`, `CALLING_PERMISSION_REQUEST`, `UTILITY CALLING_PERMISSION_REQUEST` or `OTP` |
| Time to Live | `time_to_live` (custom validity seconds, otherwise 0) |
| Header Type | `header_type` |
| Button Type | `button_summary` (e.g. `Url: https://...`, `Quick Reply: buy`, `otp: Copy Code`). `button_types` gives the raw types. |

`warnings` is a list of `[{field, message}]` and never blocks (§5).

### 2.3 Media object (`header_media`, `carousel_cards[].media`)

```json
{"s3_key": "whatsapp_templates/12/2026-11/ab12.png", "url": null, "file_name": "offer.png",
 "mime_type": "image/png", "size_bytes": 48211, "media_type": "IMAGE",
 "preview_url": "https://...signed..."}
```

- Send **either** `s3_key` (from `POST /templates/media`) **or** a public `url`.
- `preview_url` is returned by the server (a presigned link or the URL itself). It is ignored on input.
- Allowed types and sizes:

| Media | Types | Max size |
|---|---|---|
| Image | `image/jpeg`, `image/png` | 5 MB |
| Video | `video/mp4`, `video/3gpp` | 16 MB |
| Document | `application/pdf` | 100 MB |

- Carousel `media_type` is `IMAGE` or `VIDEO`, and a carousel media URL is at most 1600 characters.
- On submit, the server downloads the header file (S3 or URL) and uploads it to BroadNet as `headerfile`. URLs must be public http(s); private and loopback hosts are refused.

---

## 3. Endpoint details

### 3.1 GET /templates

Query parameters (all optional):

| Param | Meaning |
|---|---|
| `from`, `to` | Range on `created_at`. Accepts ISO (`2026-03-02T05:30:00`, `2026-03-02`, with or without an offset; offsets are converted to UTC) or BroadNet's `DD-MM-YYYY HH:mm:ss.SSS`. The UI converts the GMT dropdown into an offset or UTC. |
| `name` | Name contains this text. |
| `status` | `PENDING`, `APPROVED`, `REJECTED`, `DRAFT`, `PAUSED` or `DISABLED` (any case). `approval_status` is still accepted as an alias. |
| `language` | Exact code. |
| `category` | `MARKETING`, `UTILITY` or `AUTHENTICATION`. |
| `template_type` | `DEFAULT` (any default sub-type), `UTILITY DEFAULT` / `UTILITY_DEFAULT`, `CATALOGUE`, `CALLING_PERMISSION_REQUEST` or `OTP`. |
| `search` | Free text over the name and body. |
| `account_id` | One WhatsApp account. Drafts without an account are still listed. Returns 404 if the account is not in the org. |
| `purpose` | Clawback purpose. |
| `page`, `limit` | `limit` is 1–100 (default 25). |

Response:

```json
{"data": [<template>], "pagination": {"page": 1, "limit": 25, "total": 3, "total_pages": 1}}
```

Items are ordered by name. An unparseable date returns 422 `[{field: "from"|"to", message}]`.

### 3.2 GET /templates/options

Returns everything the editor needs, so the UI does not hard-code it:

- `categories` and `template_types` (per category)
- `variable_types` and `header_formats`
- `button_types`: a list of `{type, label, max, max_in_utility, available_in: [{category, template_type|null}]}`
- `code_delivery`, `flow_actions` and `carousel_button_types`
- `media`: `{IMAGE|VIDEO|DOCUMENT: {mime_types, max_bytes}}`
- `languages`: `[{code, label}]`
- `statuses`
- `limits` (§4.7)
- `name_pattern` and `name_error`
- `placeholder_url_warning`

### 3.3 POST /templates and PUT /templates/{id}

- **Create:** returns 200 with the template (§2.2). The new template is a draft.
- **Account used on create:** in this order:
  1. `?account_id=` or `account_id` in the body;
  2. otherwise the org's clawback / default account;
  3. otherwise any account of the org.
- **Update** replaces the whole template. It is allowed only while the status is `draft` or `rejected`; otherwise it returns 409 `"Create a new version"`. An update resets the status to `draft`.
- **Validation** is the save-level check: sample values and media are not required yet.
- **Errors:**
  - 422 with `detail: [{field, message}]`.
  - Name clash: 422 `[{field: "name", message: "A template with this name and language already exists"}]`.

### 3.4 POST /templates/validate?for_submit=true|false

Body: the template payload. Response:

```json
{"valid": false,
 "errors":   [{"field": "sample_values", "message": "Sample value required for '{{2}}'"}],
 "warnings": [{"field": "buttons[0].url", "message": "Warning: Meta may reject templates with placeholder URLs (e.g., example.com). Please use a real, valid URL."}]}
```

### 3.5 Preview: POST /templates/preview (unsaved) and POST /templates/{id}/preview

- **Unsaved body:** the template payload plus `"values": {key: value}` (optional).
- **Saved body:** `{"values": {...}}` or nothing.
- `values` override the samples. A key `header_<k>` overrides only the header variable `k`.
- A variable with no sample or value stays as raw `{{n}}`, as in BroadNet.

```json
{"header_type": "TEXT", "header": "New Arrival Shoes", "header_media": null,
 "limited_time_offer": {"text": "Expire soon", "expires_at": "2026-09-30", "has_expiration": true},
 "body": "Hi Abhishek, your order 4679 is ready.", "footer": "Reply STOP to unsubscribe",
 "buttons": [{"type": "URL", "label": "Shop now", "url": "https://shop.hti.in/o/A123", "url_example": "A123"}],
 "carousel_cards": null}
```

- **Carousel:** `carousel_cards` is `[{media: <media object>, body, buttons}]` with card samples filled in.
- **Authentication:** `body` is `"*123456* is your verification code."` plus `" For your security, do not share this code."` when security is on. `footer` is `"Expires in N minutes."` or `null`. `buttons` is `[{type: "OTP", label, otp_type}]`.
- **Location:** `header_type` is `LOCATION`. The UI draws the map placeholder; the coordinates are given at send time.
- **Rendering:** the body may contain WhatsApp formatting (`*bold*`, `_italic_`, `~strike~`, `` ```code``` ``). The UI renders it; the server returns it as is.

### 3.6 POST /templates/media

Multipart form fields:

- `file` (required).
- `media_type`: `IMAGE`, `VIDEO` or `DOCUMENT`. Optional; when given, the file must be of that type.

Super admins also pass `?organization_id=`. Responses:

| Status | Result |
|---|---|
| 200 | The media object (§2.3) with `s3_key` and `preview_url`. |
| 422 | `[{field: "file", message}]` for a wrong type or a file over the size limit. |
| 503 | File storage (S3) is not configured. |

### 3.7 POST /templates/{id}/submit

Allowed only for `draft` and `rejected` templates (otherwise 409). The submit-level validation runs first; on top of the save rules it requires:

- every header, body, URL and card variable has a sample;
- IMAGE, VIDEO and DOCUMENT headers and every carousel card have media.

It then sends the template to BroadNet through the template's account. On success it returns the template with `status: "PENDING"` (or whatever the provider reports) and `provider_template_id`.

| Status | Meaning |
|---|---|
| 422 | Validation errors, or `[{field: "header_media", message}]` when the file cannot be read or downloaded. |
| 409 | No active account: `"Connect an active WhatsApp account before submitting templates"` / `"This template's WhatsApp account is not active"`. |
| 502 | `"Provider rejected the template: <provider message>"`. |
| 500 | The stored account token cannot be decrypted. |

Warnings (e.g. a placeholder URL) do not block submit; they come back in `warnings`.

### 3.8 DELETE /templates/{id}

- Returns 204, with no status restriction (as in BroadNet).
- Returns 409 `"Template is used by notification rules"` when clawback rules point at the template.
- The template is removed from every list and get (soft delete).
- When the template was submitted, the server also asks BroadNet to delete it by name. This is best effort; a provider failure does not block the delete.
- The UI shows BroadNet's confirm modal: "Delete Template" / "This action cannot be undone." / `Are you sure you want to delete the template "<name>"?`.

---

## 4. Rules

### 4.1 Categories and sub-types (`template_type`)

| Category | Sub-types (first is the default) |
|---|---|
| MARKETING | `DEFAULT`, `CATALOGUE`, `CALLING_PERMISSION_REQUEST` |
| UTILITY | `DEFAULT`, `CALLING_PERMISSION_REQUEST` |
| AUTHENTICATION | `OTP` |

### 4.2 Variables

- **Number mode:**
  - Only `{{1}}`, `{{2}}` and so on are allowed.
  - Body numbers must run from 1 without gaps.
  - The header variable is `{{1}}` and is independent of the body's `{{1}}`; give its sample in `header_sample_values`.
  - Error message: `Only numeric variables such as {{1}} are allowed when the variable type is Number (found '{{name}}')`.
- **Name mode:**
  - Placeholders use lowercase letters, digits and `_`, and must not be all digits.
  - Error message: `Only named variables such as {{name}} are allowed when the variable type is Name (found '{{1}}')`.
- **Samples:** every variable needs a sample at submit (field `sample_values`, `header_sample_values`, `buttons[i].url_example` or `carousel_cards[i].sample_values`).

### 4.3 Buttons

All buttons share these fields: `type`, plus `label` (at most 25 characters, no variables). `text` is accepted as an alias of `label`. The table lists the extra fields per type.

| `type` | BroadNet name | Extra fields | Max | Available in |
|---|---|---|---|---|
| `PHONE_NUMBER` | Call Phone Number | `phone_number` with country code (`+911234567890`) | 1 | Marketing, Utility |
| `URL` | Visit Website | `url` (http(s), at most 2000 characters, at most one variable, at the end), `url_example` (the sample for that variable) | 2 (**1 in Utility**) | Marketing, Utility |
| `VOICE_CALL` | Voice Call | none | 1 | Marketing, Utility |
| `COPY_CODE` | Copy Offer Code | `example` (the code, at most 15 characters). `label` defaults to "Copy offer code". | 1 | Marketing |
| `MPM` | Multi-product | none | 1 | Marketing **Default** |
| `FLOW` | WhatsApp Flow | `flow_id` (required), `flow_name`, `flow_action` (`navigate` (default) or `data_exchange`), `navigate_screen` (Screen ID, required for `navigate`, at most 64 characters) | 1 | Marketing **Default** |
| `QUICK_REPLY` | Custom | none | 5 | Marketing, Utility |

- No more than 10 buttons in total.
- Authentication has no editable buttons; its OTP button comes from `auth_config`.
- An error such as `At most 1 Url button in Utility templates` is reported on the field `buttons`.
- A button that is not allowed in the current category or sub-type is reported on `buttons[i].type`.

### 4.4 Limited Time Offer

`{text, expires_at, has_expiration}` (Marketing Default only):

- `text` is required and at most 16 characters (Meta's limit).
- `expires_at` is an ISO date, used for the preview ("Offer ends on Sep 30").
- `has_expiration` is the "Offer has an expiration countdown" flag.
- Not allowed with a LOCATION or CAROUSEL header.

### 4.5 Carousel cards (`header_format: "CAROUSEL"`)

```json
[{"media": {"s3_key": "...", "media_type": "IMAGE"},
  "body_text": "Card {{1}} body", "sample_values": {"1": "A"},
  "buttons": [{"type": "URL", "label": "Check it here!", "url": "https://shop.hti.in"},
              {"type": "QUICK_REPLY", "label": "More"}]}]
```

- 1 to 10 cards.
- Card media is an upload or an image/video URL (at most 1600 characters); it is required at submit.
- `body_text` is at most 1024 characters and uses the template's variable style.
- Each card has at most 2 buttons, of type `URL`, `QUICK_REPLY` or `PHONE_NUMBER`.
- Button text is at most 25 characters and a URL is at most 2000 characters.

### 4.6 Authentication (`category: "AUTHENTICATION"`)

```json
"auth_config": {
  "code_delivery": "ONE_TAP",
  "apps": [{"package_name": "com.hti.whatsapp", "signature_hash": "K8a/AlNcGX7"}],
  "add_security_recommendation": true,
  "code_expiration_minutes": 6,
  "button_text": "Copy Code",
  "autofill_text": null
}
```

- `code_delivery` is `ZERO_TAP`, `ONE_TAP` or `COPY_CODE`.
- `apps`: 1–5 apps are required for `ZERO_TAP` and `ONE_TAP`.
  - `package_name` is at most 224 characters and in Android form (`com.example.app`).
  - `signature_hash` is exactly 11 characters.
- `code_expiration_minutes` is 1–90, or `null` (BroadNet's "Add expiry time").
- `button_text` and `autofill_text` are optional, at most 25 characters each.
- The body is fixed by Meta. The server stores `{{1}} is your verification code.` plus the security sentence when that option is on.

### 4.7 Limits

| Item | Limit |
|---|---|
| Name | 512 |
| Header | 60 |
| Body | 1024 |
| Footer | 60 |
| Button label | 25 |
| Button URL | 2000 |
| Buttons | 10 in total |
| Copy code | 15 |
| LTO text | 16 |
| Cards | 1–10 |
| Card body | 1024 |
| Card media URL | 1600 |
| Card button label | 25 |
| Card button URL | 2000 |
| Card buttons | 2 |
| Package name | 224 |
| Signature hash | 11 |
| Auth apps | 5 |
| Code expiry | 1–90 minutes |
| Validity | 30–43200 seconds |

---

## 5. Errors and warnings

- **Validation:** HTTP 422 with `{"detail": [{"field": "<path>", "message": "<text>"}]}`. Field paths are dotted or indexed, e.g. `buttons[1].url`, `carousel_cards[0].media.url`, `auth_config.apps[0].signature_hash`, `limited_time_offer.text`.
- **Other errors:** 404, 409, 502 and 503 return `{"detail": "<text>"}`.
- **Name error (exact):** `Template name must start with a lowercase letter, contain only [a-z0-9_] and be max 512 chars.`
- **Placeholder URL warning (exact, non-blocking):** `Warning: Meta may reject templates with placeholder URLs (e.g., example.com). Please use a real, valid URL.` It applies to any URL button (including card buttons) whose host is `example.com`, `example.org`, `example.net` or a subdomain of these. Show it as BroadNet's amber toast.

---

## 6. Provider mapping (BroadNet `POST /whatsapp/template`, multipart)

The following are documented in the vendor PDF (§3.1–3.14) and mapped directly:

- `name`, `languageType`, `category`.
- `templateType`: `DEFAULT`, `catalogue` or `CALLING_PERMISSION`. Utility Default is sent as `DEFAULT`; the vendor reports it back as `UTILITY_DEFAULT`.
- `subCategory`: `DEFAULT`, or `ONE_TIME_PASSCODE` for Authentication.
- `headerFormat` (`NONE`, `TEXT`, `IMAGE`, `DOCUMENT` or `VIDEO`) and `headerfile` (the file part).
- `headerText` and `headerExample` (`["x"]`).
- `bodyText` and `bodyExample` (`[["a","b"]]`). Named variables are sent as positional.
- `footerText`.
- `buttonRequest` in the form `TYPE:Label|value`, repeated once per button, for `URL`, `QUICK_REPLY` and `PHONE_NUMBER`.
- `customValidityPeriod` and `timeToLive` (minutes).

The following are **UNCONFIRMED**. The vendor PDF has no section for them (§3.15–3.27 bodies are missing), so each is a Meta-compatible guess, marked `# UNCONFIRMED` in `omnichannel.py`:

- `buttonRequest` for the other button types:
  - `VOICE_CALL:Label`
  - `COPY_CODE:Label|code`
  - `MPM:Label`
  - `FLOW:Label|flow_id|action|screen`
- A URL variable sample sent as a third segment: `URL:Label|https://x/{{1}}|sample`.
- `headerFormat=LOCATION`.
- Carousel: `headerFormat=NONE` plus `carouselCards` (JSON list of Meta card components; card media is sent as a URL, presigned for uploads).
- Limited Time Offer: `limitedTimeOffer` (JSON `{text, hasExpiration}`).
- Authentication: `authConfig` (JSON `{otpType, supportedApps[{packageName, signatureHash}], addSecurityRecommendation, codeExpirationMinutes, buttonText?, autofillText?, zeroTapTermsAccepted}`). The body is not sent. `timeToLive` is set to the code expiry minutes.
- The utility validity unit: the editor stores seconds, and the server sends `ceil(seconds/60)` minutes because the doc says minutes.
- Read, list and delete endpoints (`GET /whatsapp/template?name=`, `GET /whatsapp/template/list`, `DELETE /whatsapp/template/{name}`). These existed before this change; delete is best effort.

Nothing is refused at submit because of these guesses. If BroadNet rejects a field, submit returns 502 with BroadNet's message.

## Approval status updates and notifications

Omnichannel forwards Meta's webhook body unchanged to the account's webhook
URL. CONFIRMED on staging: `message_template_status_update`, `messages`
(statuses) and `flows`. `message_template_quality_update` and
`template_category_update` follow Meta's documented shape (not yet seen from
Omnichannel).

Webhook events and the 15-minute sync both go through
`template_status.apply_change`, so they behave the same:

| Meta event | `approval_status` | Notified |
|---|---|---|
| `APPROVED` | `approved` | "approved — you can now use it in clawback and campaigns" |
| `REINSTATED` (or approved after paused) | `approved` | "active again" |
| `FLAGGED` | `approved` (still sendable), `quality_score` = `RED` | quality dropped |
| `PENDING`, `IN_APPEAL` | `pending` | no |
| `REJECTED` | `rejected`, `rejection_reason` = Meta's reason | "rejected: <reason>" |
| `PAUSED`, `LIMIT_EXCEEDED` | `paused`, reason from `other_info` | what to do |
| `DISABLED`, `PENDING_DELETION`, `DELETED` | `disabled` | create a new version |

Quality drops to `YELLOW` / `RED` notify; a category change sets
`previous_category` / `category` and notifies. A template that was still a
`draft` here (imported) changes quietly.

Recipients: the user who submitted the template (`updated_by` at submit) and
its creator (`created_by`), plus org admins, as in-app notifications with live
push (there is no email path for these alerts). `metadata.link` is
`/settings/channels/whatsapp/<account_id>/templates/<template_id>`; the bell
opens it. Each value is notified once per template (Redis, 7 days); moving to
another value releases the old key, so approved → paused → approved notifies
again. Toggles: `system:alert_template_rejected` (rejections) and
`system:alert_template_status` (everything else).

Flow alerts (`flows`) notify org admins once per flow + event while
`alert_state` is `ACTIVATED`; `DEACTIVATED` / `RESOLVED` clears it (and says it
recovered). Toggle `system:alert_flow_endpoint`. They are forwarded to the
Campaign Service, which stores the alert on its flow.
