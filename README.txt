CONTINENTE POKEMON ALERTS - CLOUD VERSION

Prepared for GitHub Actions. This package is not deployed yet.

The scheduled cloud job works independently of your PC and sends alerts to the
same ntfy phone subscription. It reuses the tested product matching and stock
confirmation logic from the Windows watcher.

FREE HOSTING
Standard GitHub Actions runners are free for public repositories. This workflow
only runs in a public repository, uses the standard Ubuntu runner, and does not
upload artifacts or create caches. It skips private repositories to avoid
consuming a potentially paid private Actions allowance.

The repository's code, baseline and remembered public product data are public.
Your notification topic is stored as an Actions secret, never in repository
files. Do not upload the Windows config.json, SUBSCRIBE.txt or runtime files.

SCHEDULING LIMITS
The workflow requests a check every five minutes. GitHub can delay or drop
scheduled runs, so this does not guarantee detecting a drop within five minutes.
Scheduled workflows must be on the repository's default branch. GitHub can
disable public schedules after 60 days without repository activity; legitimate
state updates normally create ongoing repository activity here.

DEPLOYMENT
1. Create a public GitHub repository for this bot (for example,
   continente-pokemon-alerts). Confirm that you are happy to publish the code
   and public product baseline before doing this.
2. Upload all files from this folder, including .github/workflows/pokemon.yml
   and .gitignore, onto its default branch. Do not add notification settings.
3. Under Settings > Secrets and variables > Actions, add a repository secret
   named NTFY_TOPIC. Its value is your existing topic, shown in the Windows
   version's SUBSCRIBE.txt. Never put a GitHub token or topic into source files.
4. Under Actions, enable workflows if GitHub asks. The workflow's contents:write
   permission is required to save product history on the monitor-state branch.
5. Open Actions > Continente Pokemon alerts > Run workflow. Check that the run
   finishes successfully and creates the monitor-state branch.
6. Once the cloud test succeeds, run STOP.cmd in the Windows watcher folder.
   Otherwise both copies could send the same alerts.
7. Verify that a scheduled cloud run also succeeds. The PC can then be turned
   off; your phone still needs internet access for notifications.

The cloud package includes a snapshot of your existing public product history,
so old listings do not all generate alerts again. Deployment after a long delay
may generate genuine changes since that snapshot.

HOW STATE SURVIVES
Each cloud runner is temporary. cloud_run.py loads the last saved state from
.monitor-state.json on the monitor-state branch. It persists the notification
queue before sending and updates it after each acknowledged message. GitHub
storage failures stop sending rather than discarding the history. Simultaneous
runs are serialized. As with most webhook systems, rare duplicate notification
delivery is possible after an ambiguous network failure.

STATUS / STOPPING
Open the repository's Actions tab to see checks and failures. After three
consecutive failures the watcher attempts an ntfy health alert, limited to once
per six hours. If notifications or GitHub itself are unavailable, that alert
may not arrive, so the Actions tab is the authoritative run status.
To stop cloud monitoring, disable the workflow in Actions. This is independent
of STOP.cmd, which only controls the Windows copy.

LOCAL CHECKS
  py -3.12 -m unittest discover -s tests -v

No third-party Python packages are required. Actions and its permissions must
be verified in the actual GitHub account before this can be called deployed.

OFFICIAL REFERENCES
Free public runners:
https://docs.github.com/en/billing/concepts/product-billing/github-actions
Schedules, delays, and inactivity:
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
GitHub contents API:
https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
