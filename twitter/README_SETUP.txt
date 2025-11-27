# Twitter Repost Bot — Updated (no goodgirlxsz)

This package is configured to use 2 source accounts: shiyohost, ghostonki and conservative defaults.
Files:
- post_repost.py  (full script)
- requirements.txt
- .github/workflows/post-schedule.yml  (7-run schedule)
- .env.example (placeholders)
- posted_history.json

Setup:
1. Upload these files to your GitHub repo.
2. In Settings → Secrets → Actions add/update:
   - X_BEARER_TOKEN
   - X_CONSUMER_KEY
   - X_CONSUMER_SECRET
   - X_ACCESS_TOKEN  (must have Read+Write)
   - X_ACCESS_SECRET (must have Read+Write)
   - SOURCE_USERNAMES (set to "shiyohost,ghostonki")
3. Run Actions → scheduled-repost → Run workflow once to test.
