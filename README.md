# PlaneAlert cloud logger

Runs on GitHub Actions every 5–15 minutes, around the clock. It logs arrivals and departures at the airports in `logger/watch.txt` using the free adsb.lol and adsb.fi feeds. Each day's log is saved under **Releases → data** as `day-YYYY-MM-DD.json.gz`, and the last 7 days are kept.

The PlaneAlert app downloads today's log and merges it, so the day list is complete even when the PC was off.

## Setup

1. **Create the repository.** On GitHub, click **New repository**, name it `planealert-log`, choose **Public**, and click **Create repository**.
2. **Upload the files.** Click **uploading an existing file**. Drag in the `.github` folder, the `logger` folder and this README, then click **Commit changes**.
3. **Start the logger.** Open the **Actions** tab. If GitHub asks, enable workflows. Click **PlaneAlert logger**, then **Run workflow**. After about a minute, a release called `data` appears with today's log.
4. **Connect the app.** In PlaneAlert, click **Cloud log** and enter `your-username/planealert-log`.

## Changing airports

Edit `logger/watch.txt` on GitHub (pencil icon) and commit. Keep it the same as the airports in the app.

## Troubleshooting

- **A run fails at "Upload log" with "Resource not accessible":** go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and save.
- **The timer stops:** GitHub pauses timers in repositories with no activity for 60 days. The logger commits `logger/heartbeat.txt` once a day to prevent this. If it ever stops, open **Actions** and enable the workflow again.
