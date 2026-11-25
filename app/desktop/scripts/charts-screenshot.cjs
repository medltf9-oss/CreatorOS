// Electron main process for the P8.18/P8.19 preview screenshots.
//
// Loads the built dev-only harness pages (dist-charts/*.html) and captures PNGs into docs/evidence.
// It renders the *real* chart and analytics page components; every fixture behind them is a labelled
// synthetic stub, so nothing reaches the production database.
//
// The offscreen surface is clamped to the screen height, so a tall page is captured at 100% zoom as
// successive scrolled sections (the first section keeps the page's plain name, later ones get -2, -3).
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const projectRoot = path.join(__dirname, '..');
const outDir = process.argv[2] || path.join(projectRoot, '..', '..', 'docs', 'evidence');

// file, query, width, height, outputs (one per vertical section)
const SHOTS = [
  ['charts-preview.html', 'shot=populated', 1000, 560, ['p8-chart-populated.png']],
  ['charts-preview.html', 'shot=gaps', 1000, 560, ['p8-chart-gaps.png']],
  ['charts-preview.html', 'shot=unavailable', 1000, 560, ['p8-chart-unavailable.png']],
  ['charts-preview.html', 'shot=zero', 1000, 560, ['p8-chart-zero.png']],
  ['charts-preview.html', 'shot=series', 1000, 560, ['p8-chart-multiseries.png']],
  ['analytics-preview.html', 'shot=overview', 1320, 700, ['p8-analytics-overview.png', 'p8-analytics-overview-2.png']],
  ['analytics-preview.html', 'shot=compare', 1320, 700, ['p8-analytics-platform-comparison.png', 'p8-analytics-platform-comparison-2.png']],
  ['analytics-preview.html', 'shot=account', 1320, 700, ['p8-analytics-account-performance.png', 'p8-analytics-account-performance-2.png']],
  ['analytics-preview.html', 'shot=loading', 1320, 520, ['p8-analytics-loading.png']],
  ['analytics-preview.html', 'shot=nodata', 1320, 700, ['p8-analytics-nodata.png', 'p8-analytics-nodata-2.png']],
  ['analytics-preview.html', 'shot=permission', 1320, 700, ['p8-analytics-permission.png', 'p8-analytics-permission-2.png']],
  ['analytics-preview.html', 'shot=responsive', 560, 1030, ['p8-analytics-responsive.png', 'p8-analytics-responsive-2.png']],
  // P8.21 — publication / content / campaign pages.
  ['analytics-preview.html', 'shot=publication', 1320, 720, ['p8-analytics-publication.png', 'p8-analytics-publication-2.png']],
  ['analytics-preview.html', 'shot=publication-unavailable', 1320, 720, ['p8-analytics-publication-unavailable.png', 'p8-analytics-publication-unavailable-2.png']],
  ['analytics-preview.html', 'shot=retention', 1320, 720, ['p8-analytics-retention.png', 'p8-analytics-retention-middle.png', 'p8-analytics-retention-bottom.png']],
  ['analytics-preview.html', 'shot=publication-loading', 1320, 520, ['p8-analytics-publication-loading.png']],
  ['analytics-preview.html', 'shot=content', 1320, 720, ['p8-analytics-content-comparison.png', 'p8-analytics-content-comparison-2.png']],
  ['analytics-preview.html', 'shot=content-nodata', 1320, 560, ['p8-analytics-content-nodata.png']],
  ['analytics-preview.html', 'shot=campaign', 1320, 720, ['p8-analytics-campaign.png', 'p8-analytics-campaign-2.png']],
  ['analytics-preview.html', 'shot=campaign-partial', 1320, 720, ['p8-analytics-campaign-partial.png', 'p8-analytics-campaign-partial-2.png']],
  ['analytics-preview.html', 'shot=campaign-loading', 1320, 520, ['p8-analytics-campaign-loading.png']],
  ['analytics-preview.html', 'shot=content', 620, 1100, ['p8-analytics-content-responsive.png', 'p8-analytics-content-responsive-2.png']],
  // P8.22 — video performance / audience growth / hashtag analysis.
  ['analytics-preview.html', 'shot=video', 1320, 720, ['p8-analytics-video.png', 'p8-analytics-video-2.png', 'p8-analytics-video-3.png']],
  ['analytics-preview.html', 'shot=video-detail', 1320, 720, ['p8-analytics-video-retention.png', 'p8-analytics-video-retention-2.png', 'p8-analytics-video-retention-3.png']],
  ['analytics-preview.html', 'shot=video-no-retention', 1320, 720, ['p8-analytics-video-no-retention.png', 'p8-analytics-video-no-retention-2.png']],
  ['analytics-preview.html', 'shot=video-compare', 1320, 720, ['p8-analytics-video-comparison.png', 'p8-analytics-video-comparison-2.png']],
  ['analytics-preview.html', 'shot=video-loading', 1320, 520, ['p8-analytics-video-loading.png']],
  ['analytics-preview.html', 'shot=video-nodata', 1320, 560, ['p8-analytics-video-nodata.png']],
  ['analytics-preview.html', 'shot=audience', 1320, 720, ['p8-analytics-audience.png', 'p8-analytics-audience-2.png', 'p8-analytics-audience-3.png']],
  ['analytics-preview.html', 'shot=audience-multi', 1320, 720, ['p8-analytics-audience-selected.png', 'p8-analytics-audience-selected-2.png']],
  ['analytics-preview.html', 'shot=audience-nohistory', 1320, 720, ['p8-analytics-audience-nohistory.png', 'p8-analytics-audience-nohistory-2.png']],
  ['analytics-preview.html', 'shot=audience-permission', 1320, 720, ['p8-analytics-audience-permission.png', 'p8-analytics-audience-permission-2.png']],
  ['analytics-preview.html', 'shot=hashtags', 1320, 720, ['p8-analytics-hashtags.png', 'p8-analytics-hashtags-2.png', 'p8-analytics-hashtags-3.png']],
  ['analytics-preview.html', 'shot=hashtags-detail', 1320, 720, ['p8-analytics-hashtag-details.png', 'p8-analytics-hashtag-details-2.png', 'p8-analytics-hashtag-details-3.png']],
  ['analytics-preview.html', 'shot=hashtags-nodata', 1320, 560, ['p8-analytics-hashtags-nodata.png']],
  ['analytics-preview.html', 'shot=video', 620, 1100, ['p8-analytics-video-responsive.png', 'p8-analytics-video-responsive-2.png']],
  ['analytics-preview.html', 'shot=hashtags', 620, 1100, ['p8-analytics-hashtags-responsive.png', 'p8-analytics-hashtags-responsive-2.png']],
  // P8.23 — recommendations / reports / analytics settings.
  ['analytics-preview.html', 'shot=recommendations', 1320, 720, ['p8-analytics-recommendations.png', 'p8-analytics-recommendations-2.png', 'p8-analytics-recommendations-3.png']],
  ['analytics-preview.html', 'shot=recommendations-accept', 1320, 720, ['p8-analytics-recommendations-accept.png', 'p8-analytics-recommendations-accept-2.png']],
  ['analytics-preview.html', 'shot=recommendations-reject', 1320, 720, ['p8-analytics-recommendations-reject.png', 'p8-analytics-recommendations-reject-2.png']],
  ['analytics-preview.html', 'shot=recommendations-decided', 1320, 720, ['p8-analytics-recommendations-decided.png', 'p8-analytics-recommendations-decided-2.png']],
  ['analytics-preview.html', 'shot=recommendations-superseded', 1320, 720, ['p8-analytics-recommendations-superseded.png']],
  ['analytics-preview.html', 'shot=recommendations-defect', 1320, 720, ['p8-analytics-recommendations-defect.png', 'p8-analytics-recommendations-defect-2.png']],
  ['analytics-preview.html', 'shot=recommendations-nodata', 1320, 560, ['p8-analytics-recommendations-nodata.png']],
  ['analytics-preview.html', 'shot=recommendations-loading', 1320, 520, ['p8-analytics-recommendations-loading.png']],
  ['analytics-preview.html', 'shot=reports', 1320, 720, ['p8-analytics-reports.png', 'p8-analytics-reports-2.png', 'p8-analytics-reports-3.png']],
  ['analytics-preview.html', 'shot=reports-verified', 1320, 900, ['p8-analytics-reports-verified.png', 'p8-analytics-reports-verified-2.png', 'p8-analytics-reports-verified-3.png']],
  ['analytics-preview.html', 'shot=reports-mismatch', 1320, 800, ['p8-analytics-reports-mismatch.png', 'p8-analytics-reports-mismatch-2.png']],
  ['analytics-preview.html', 'shot=reports-nohash', 1320, 760, ['p8-analytics-reports-nohash.png', 'p8-analytics-reports-nohash-2.png']],
  ['analytics-preview.html', 'shot=reports-nodata', 1320, 700, ['p8-analytics-reports-nodata.png']],
  ['analytics-preview.html', 'shot=reports-error', 1320, 560, ['p8-analytics-reports-error.png']],
  ['analytics-preview.html', 'shot=settings', 1320, 720, ['p8-analytics-settings.png', 'p8-analytics-settings-2.png', 'p8-analytics-settings-3.png']],
  ['analytics-preview.html', 'shot=settings-loading', 1320, 480, ['p8-analytics-settings-loading.png']],
  ['analytics-preview.html', 'shot=settings-error', 1320, 560, ['p8-analytics-settings-error.png']],
  ['analytics-preview.html', 'shot=reports-csv', 1320, 900, ['p8-analytics-reports-csv.png', 'p8-analytics-reports-csv-2.png', 'p8-analytics-reports-csv-3.png']],
  ['analytics-preview.html', 'shot=settings-retention', 1320, 900, ['p8-analytics-settings-retention.png', 'p8-analytics-settings-retention-2.png', 'p8-analytics-settings-retention-3.png']],
  ['analytics-preview.html', 'shot=settings-retention-executed', 1320, 900, ['p8-analytics-settings-retention-executed.png', 'p8-analytics-settings-retention-executed-2.png', 'p8-analytics-settings-retention-executed-3.png']],
  ['analytics-preview.html', 'shot=recommendations', 620, 1100, ['p8-analytics-recommendations-responsive.png', 'p8-analytics-recommendations-responsive-2.png']],
  ['analytics-preview.html', 'shot=settings', 620, 1100, ['p8-analytics-settings-responsive.png', 'p8-analytics-settings-responsive-2.png']],
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function waitForPaint(win, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    win.webContents.once('paint', finish);
    setTimeout(finish, timeoutMs);
  });
}

async function captureWithRetry(win, attempts = 8) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await win.webContents.capturePage();
    } catch (error) {
      lastError = error;
      await sleep(300);
    }
  }
  throw lastError ?? new Error('capturePage failed');
}

async function loadWithRetry(win, url, attempts = 4) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      await win.loadURL(url);
      return;
    } catch (error) {
      lastError = error;
      await sleep(400);
    }
  }
  throw lastError ?? new Error(`could not load ${url}`);
}

async function scrollTo(win, y) {
  await win.webContents.executeJavaScript(`window.scrollTo(0, ${Math.max(0, Math.round(y))}); true`);
  await waitForPaint(win, 800);
  await sleep(180);
}

// This shell has no GPU compositor, so Electron needs the bundled software rasteriser.
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('enable-unsafe-swiftshader');

// Do not quit when a capture window is destroyed: each scenario uses its own window.
app.on('window-all-closed', () => {});

app
  .whenReady()
  .then(async () => {
    const distCharts = path.join(projectRoot, 'dist-charts');
    fs.mkdirSync(outDir, { recursive: true });

    for (const [file, query, width, height, outputs] of SHOTS) {
      const target = path.join(distCharts, file);
      if (!fs.existsSync(target)) {
        console.error('[preview] build the harness first: pnpm charts:preview');
        app.exit(2);
        return;
      }
      const win = new BrowserWindow({
        width,
        height,
        show: false,
        webPreferences: { offscreen: true, backgroundThrottling: false },
      });
      await loadWithRetry(win, `${pathToFileURL(target).toString()}?${query}`);
      await waitForPaint(win);
      await sleep(250);

      await scrollTo(win, 0);
      const maxScroll = await win.webContents.executeJavaScript('Math.max(0, document.documentElement.scrollHeight - window.innerHeight)');

      const image = await captureWithRetry(win);
      fs.writeFileSync(path.join(outDir, outputs[0]), image.toPNG());
      console.log('[preview] saved', path.join(outDir, outputs[0]), `${image.getSize().width}x${image.getSize().height}`);

      if (outputs.length > 1 && maxScroll > 4) {
        if (outputs.length > 2) {
          // A three-name shot captures the middle section too: some cards (a retention curve) sit
          // between the top and the bottom and would otherwise appear in neither screen.
          await scrollTo(win, Math.round(maxScroll * 0.5));
          const middle = await captureWithRetry(win);
          fs.writeFileSync(path.join(outDir, outputs[1]), middle.toPNG());
          console.log('[preview] saved', path.join(outDir, outputs[1]), `${middle.getSize().width}x${middle.getSize().height}`);
          await scrollTo(win, maxScroll);
          const bottom = await captureWithRetry(win);
          fs.writeFileSync(path.join(outDir, outputs[2]), bottom.toPNG());
          console.log('[preview] saved', path.join(outDir, outputs[2]), `${bottom.getSize().width}x${bottom.getSize().height}`);
        } else {
          await scrollTo(win, maxScroll);
          const scrolled = await win.webContents.executeJavaScript('window.scrollY');
          if (scrolled > 2) {
            const second = await captureWithRetry(win);
            fs.writeFileSync(path.join(outDir, outputs[1]), second.toPNG());
            console.log('[preview] saved', path.join(outDir, outputs[1]), `${second.getSize().width}x${second.getSize().height}`);
          }
        }
      }
      win.destroy();
      await sleep(250);
    }
    app.quit();
  })
  .catch((error) => {
    console.error('[preview] capture failed:', error);
    app.exit(1);
  });
