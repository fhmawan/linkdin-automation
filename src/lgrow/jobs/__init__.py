"""Job discovery.

Every source is a documented public JSON API. LinkedIn is deliberately absent:
scraping it is the single biggest cause of account suspension, and these feeds
cover the same remote market. See RISKS.md.

Several sources ask for attribution in their terms (Remotive, Jobicy, RemoteOK).
We honour that by always keeping the original posting URL and sending you there
to apply — we never re-host or re-publish their listings.
"""
