# Security policy

## Supported versions

Security fixes are applied to the latest release on `main`.

## Report a vulnerability

Please do not open a public issue for a suspected vulnerability. Email
`security@makepay.io` with:

- the affected version or commit;
- clear reproduction steps;
- expected and observed impact;
- any suggested remediation.

Do not include live API keys, webhook secrets, Telegram bot tokens, wallet
credentials, or other third-party personal data. Use test credentials and
redacted request samples.

We aim to acknowledge a report within three business days. We will coordinate
validation, remediation, release timing, and credit with the reporter.

## Security boundaries

This starter:

- creates MakePay hosted payment links;
- keeps Telegram identity and order mapping in the local database;
- verifies Telegram and MakePay webhook authenticity;
- never requests or stores seed phrases, private keys, or payer wallet secrets.

The operator remains responsible for host security, TLS termination, secret
storage, database backups, MakePay account configuration, fulfillment, support,
and legal/compliance obligations.

