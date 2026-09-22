# emailer

An email API in the style of EmailJS. A customer signs up, verifies their own domains, gets an API key, and sends email from their website or server through Amazon SES. They can only send from domains they have verified.

The dashboard for it is a separate repo, `../emailer-fe`.

## How it works

```
Customer site or server
   POST /v1/send  (x-api-key)
        │
   API Gateway ──► validate Lambda ──► SQS queue ──► worker Lambda ──► Amazon SES
                        │                                  │
                        └────────────► DynamoDB ◄──────────┘
                                          ▲
Dashboard (Cognito login)                 │
   /admin/*  ──► API Gateway ──► admin Lambda, domains Lambda
```

| Piece | What it does |
|---|---|
| `src/validate` | The public send endpoint. Checks the API key, the allowed websites, that the `from_email` domain is verified and owned by the key's account, and the daily limit. Then it writes the email record and queues the job. |
| `src/worker` | Takes jobs off the queue, renders the Jinja2 template, and sends through SES. Temporary failures are retried, and after 3 attempts a job goes to the dead-letter queue. |
| `src/admin` | Login-protected routes for the API key, templates, and email status and history. |
| `src/domains` | Login-protected routes to add and verify domains in SES. It also runs on a 15-minute schedule to re-check every domain. |
| `infra/` | Terraform for everything above, plus Cognito, DynamoDB, the queue and alarms. |

There is one DynamoDB table and no GSIs. The worker is not behind API Gateway. The queue triggers it.

## The model

- An **account** is one Cognito login (`acct_<token sub>`). Everything is scoped to it, and another account's data always looks like "not found".
- An account can verify any number of **domains**. A domain is verified when its DKIM records check out in SES.
- An account has **one API key**. Creating a new key turns the old one off. The full key is returned once, when it's created, and can't be read back. The key isn't tied to a domain or an address.
- Every send names its own **`from_email`**. It must be a plain address (`hi@acme.com`, no display name) on a domain the account has verified. It's an exact match: `mail.acme.com` needs its own verification.
- **Templates** belong to the account. They're Jinja2, with `to_email` as the only required parameter. The HTML body is auto-escaped, so a visitor's text can't inject markup.
- Emails are kept for 30 days. Each key can send 500 a day.

## API

### Send: `POST /v1/send`

```
x-api-key: gk_...
Content-Type: application/json

{
  "from_email": "no-reply@acme.com",
  "template_id": "welcome",
  "template_params": { "to_email": "someone@example.com", "to_name": "Sam" },
  "attachments": [{ "object_key": "users/acct_.../<uuid>_invoice.pdf", "filename": "invoice.pdf" }]
}
```

`attachments` is optional and, on plans that allow it, comes from `POST /v1/attachments/upload-url` below.

| Status | Meaning |
|---|---|
| `202` | Queued. The body has `request_id`. Delivery happens afterwards. |
| `400` | A field is missing: `from_email`, `template_id`, `template_params` or `template_params.to_email`. |
| `401` | The key is missing, wrong, or was replaced by a newer one. |
| `403` | The website isn't in the key's allowed websites, `from_email` isn't a verified domain of the account, or an attachment isn't allowed on the plan, isn't this account's, is missing, or is over the plan's total. |
| `429` | The key hit its plan's monthly request limit. |
| `500` | The job couldn't be queued. |

### Attachments: `POST /v1/attachments/upload-url`

Same `x-api-key` header as `/v1/send`. Takes `{"filename": "invoice.pdf"}` and returns a presigned S3 upload, capped by the account's plan (`403` if the plan doesn't allow attachments):

```
{ "upload_url": "https://...", "fields": { ... }, "object_key": "users/acct_.../<uuid>_invoice.pdf" }
```

POST the file straight to `upload_url` with `fields` as form fields (S3 rejects anything over the plan's attachment size), then pass the returned `object_key` in `/v1/send`. Files are deleted right after the email sends (or fails for good); a 1-day bucket lifecycle rule is the backstop.

### Admin: `Authorization: Bearer <Cognito access token>`

| Route | Purpose |
|---|---|
| `GET /admin/keys` | The account's key as a masked hint (`gk_*******abcd`), its active flag, created date and allowed websites. Never the full key. |
| `POST /admin/keys` | Create the key `{allowed_origins?}`, and turn the previous one off. Returns the full key once. |
| `GET /admin/templates`, `POST /admin/templates` | List and create templates. |
| `GET`, `PUT`, `DELETE /admin/templates/{id}` | Read, update and delete one template. |
| `GET /admin/emails?limit=&cursor=` | The account's emails, newest first, 25 per page (max 50). |
| `GET /admin/emails/{request_id}` | One email's status, attempts and last error. |
| `GET /admin/usage/{api_key}` | Daily counts for a key. It takes the raw key in the path, so the dashboard doesn't use it. |
| `POST /admin/domains`, `GET /admin/domains` | Add a domain (returns the DNS records to publish) and list domains. |
| `GET`, `DELETE /admin/domains/{domain}` | One domain with its records, and remove it. |
| `POST /admin/domains/{domain}/check` | Re-read the status from SES now (at most every 10 seconds per domain). |
| `POST /admin/domains/{domain}/retry` | Restart a failed DKIM or bounce-domain check. |

## Repo layout

```
src/
  validate/  worker/  admin/  domains/     one Lambda each (Python 3.12)
infra/
  environments/pilot/                       the deployed stack: variables, outputs, tfvars
  modules/  api  email  queue  storage  worker
tests/                                      offline unit tests
scripts/
  send_test_email.py                        send one real email through the API
  seed.py                                   bootstrap an account, key and template by hand
dev-console.html                            a bare-bones browser tool for poking the API
docs/                                       design notes (kept out of git)
```

## Tests

The tests run entirely offline. DynamoDB is faked with `moto`, SES is mocked, and they use fake credentials, so they can't touch AWS.

```bash
python -m venv .venv
.venv/Scripts/activate        # macOS and Linux: source .venv/bin/activate
pip install -r tests/requirements.txt
python -m pytest tests
```

Use a virtual environment. A global Python with unrelated pytest plugins installed can break test collection.

## Deploy

Terraform runs locally, and its state file stays on the machine that applies. Nothing is deployed by CI.

```bash
terraform -chdir=infra/environments/pilot plan
terraform -chdir=infra/environments/pilot apply
```

- Needs AWS credentials for the target account, and `pip` on the path, because the Lambda layers are built locally.
- The pilot runs in `us-east-1`. Scripts that call AWS directly need `AWS_DEFAULT_REGION=us-east-1` set, because your local default may differ.
- `infra/environments/pilot/terraform.tfvars` holds the sender addresses to verify in SES and the alarm email.
- `terraform output` gives the values the dashboard and the scripts need: `api_url`, `user_pool_client_id` and the rest.
- Deploy when the send queue is empty. A job queued under an older message format fails.
- The pilot is in the SES sandbox, so it only delivers to verified recipient addresses until production access is granted. A new AWS account starts in the sandbox again.

## Try it

**Send an email.** `scripts/send_test_email.py` is the same Python snippet the dashboard's Integrate page shows. It reads `EMAILER_API_KEY` from a `.env` file next to it. `.env` files are git-ignored, so the key stays out of the repo.

```bash
pip install requests python-dotenv
python scripts/send_test_email.py
```

```
# scripts/.env
EMAILER_API_KEY=gk_...
```

It prints `202` and a request ID once the email is queued. Look the ID up on the dashboard's Emails page to see whether it was delivered.

**Poke the API by hand.** Open `dev-console.html` in a browser. It's prefilled with the pilot's API URL and Cognito client ID, and does sign-up, login, domains, templates and sending against the real backend.

## Things to know

- Sender addresses verified one by one in SES (the pilot's Gmail testers) no longer work through the API. A sender must be on a domain an account has verified.
- The worker doesn't re-check the domain. If a domain stops being verified after an email is queued, SES rejects it and the email shows as failed.
- The pilot will move to a different AWS account. Nothing is migrated: logins, data, domains and DNS records are all set up again. Never hard-code the API URL or the Cognito client ID.
