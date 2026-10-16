# Paddle fixtures

Each `*.json` file is the verbatim "Request example" / "Response example" JSON block from Paddle's own API
reference (fetched 16 Oct 2026 via the pages' `.md` siblings). Tests copy and edit them in code (status,
amount, reason) and say what they changed; the files themselves are not edited.

| File | Source |
|---|---|
| create-adjustment.request.json / .response.json | https://developer.paddle.com/api-reference/adjustments/create-adjustment |
| list-adjustments.response.json | https://developer.paddle.com/api-reference/adjustments/list-adjustments |
| cancel-subscription.response.json | https://developer.paddle.com/api-reference/subscriptions/cancel-subscription |
| get-subscription.response.json | https://developer.paddle.com/api-reference/subscriptions/get-subscription |
| get-transaction.response.json | https://developer.paddle.com/api-reference/transactions/get-transaction |
| list-transactions.response.json | https://developer.paddle.com/api-reference/transactions/list-transactions |
| get-customer.response.json | https://developer.paddle.com/api-reference/customers/get-customer |
