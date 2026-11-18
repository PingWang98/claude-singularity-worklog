'use strict';
// Trusted, read-only headless browser entry. No API credentials or browser flags
// come from the page or command line. Chromium's native sandbox stays enabled.
const fs = require('fs');
const path = require('path');
const {chromium} = require('/opt/browser/playwright');

function deviceInfo() {
  return {timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    intl_locale: Intl.DateTimeFormat().resolvedOptions().locale,
    language: navigator.language, languages: navigator.languages,
    offset_minutes: -new Date().getTimezoneOffset(), user_agent: navigator.userAgent,
    webdriver: navigator.webdriver};
}

async function main() {
  const target = new URL(process.argv[2]);
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password)
    throw new Error('Invalid browser URL');
  const region = JSON.parse(fs.readFileSync('/run/claude-safe/region.json', 'utf8'));
  const proxy = new URL(process.env.HTTP_PROXY);
  const output = path.join('/workspace/browser-results', new Date().toISOString().replace(/[:.]/g, '-'));
  fs.mkdirSync(output, {recursive: true, mode: 0o700});
  const report = {target: target.href, region, started: new Date().toISOString(),
    responses: [], failed_requests: [], page_errors: []};
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: '/opt/browser/usr/lib/chromium/chromium',
      headless: true, chromiumSandbox: true, timeout: 25000,
      proxy: {server: proxy.protocol + '//' + proxy.host,
        username: decodeURIComponent(proxy.username), password: decodeURIComponent(proxy.password)},
      env: {...process.env,
        LD_LIBRARY_PATH: '/opt/browser/usr/lib/x86_64-linux-gnu:/opt/browser/lib/x86_64-linux-gnu:/opt/browser/usr/lib/x86_64-linux-gnu/pulseaudio:/opt/browser/usr/lib/chromium',
        FONTCONFIG_FILE: '/opt/browser/fonts.conf'},
      args: ['--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-quic',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--proxy-bypass-list=<-loopback>', '--lang=' + region.language_tag,
        '--accept-lang=' + region.language_tag]
    });
    report.browser_version = browser.version();
    const context = await browser.newContext({viewport: {width: 1440, height: 1100},
      locale: region.language_tag});
    const page = await context.newPage();
    await page.goto('chrome://sandbox');
    report.chromium_sandbox = await page.locator('body').innerText();
    // This is Chromium's own status, in addition to the outer container policy.
    if (!(/Namespace Sandbox\s+Yes/i.test(report.chromium_sandbox) ||
          /Layer 1 Sandbox\s+Namespace/i.test(report.chromium_sandbox)) ||
        !/PID namespaces\s+Yes/i.test(report.chromium_sandbox) ||
        !/Network namespaces\s+Yes/i.test(report.chromium_sandbox) ||
        !/Seccomp-BPF sandbox\s+Yes/i.test(report.chromium_sandbox))
      throw new Error('Chromium native namespace/seccomp sandbox is not active');
    await fs.promises.writeFile(path.join(output, 'sandbox.txt'), report.chromium_sandbox);
    await page.goto('about:blank');
    report.device_before_navigation = await page.evaluate(deviceInfo);
    const expectedTimezone = Intl.DateTimeFormat('en', {timeZone: region.timezone}).resolvedOptions().timeZone;
    const before = report.device_before_navigation;
    if (before.timezone !== expectedTimezone || before.language !== region.language_tag ||
        before.intl_locale !== Intl.getCanonicalLocales(region.language_tag)[0] ||
        before.offset_minutes !== -new Date().getTimezoneOffset())
      throw new Error('Browser timezone/language verification failed before navigation');
    const pending = new Set();
    page.on('response', response => {
      const u = new URL(response.url());
      if (u.hostname === 'ip.net.coffee' && /^\/api\/(dns\/result|iprisk|geoip)\//.test(u.pathname)) {
        const job = response.json().then(data => report.responses.push({url: u.href,
          status: response.status(), data})).catch(() => {});
        pending.add(job); job.finally(() => pending.delete(job));
      }
    });
    page.on('requestfailed', request => report.failed_requests.push({url: request.url(),
      error: request.failure()?.errorText}));
    page.on('pageerror', error => report.page_errors.push(error.message));
    const response = await page.goto(target.href, {waitUntil: 'domcontentloaded', timeout: 40000});
    report.http_status = response.status();
    if (target.hostname === 'ip.net.coffee' && target.pathname.startsWith('/claude')) {
      await page.waitForFunction(() => {
        const ids = ['gaugeScore', 'dnsLeakContent', 'udpLeakContent', 'deviceContent'];
        return ids.every(id => {const t = document.getElementById(id)?.innerText || '';
          return t.trim() && !/检测中|加载中|等待中/.test(t);});
      }, null, {timeout: 45000}).catch(() => {report.incomplete = true;});
      // Allow progressive network/risk rendering to finish, even if placeholders differ.
      await page.waitForTimeout(12000);
      report.site = await page.evaluate(() => Object.fromEntries(
        ['gaugeScore', 'dnsLeakContent', 'udpLeakContent', 'deviceContent', 'claudeAvailContent']
          .map(id => [id, document.getElementById(id)?.innerText || null])));
    } else {
      await page.waitForTimeout(3000);
    }
    report.device = await page.evaluate(deviceInfo);
    report.rtc = await page.evaluate(async () => {
      const result = {api_available: typeof RTCPeerConnection === 'function', candidates: [], errors: []};
      if (!result.api_available) return result;
      let pc;
      try {
        pc = new RTCPeerConnection({iceServers: [
          {urls: 'stun:stun.l.google.com:19302'}, {urls: 'stun:stun.cloudflare.com:3478'}]});
        pc.onicecandidate = event => {if (event.candidate) result.candidates.push(event.candidate.toJSON());};
        pc.onicecandidateerror = event => result.errors.push({code: event.errorCode, text: event.errorText});
        pc.createDataChannel('diagnostic');
        await pc.setLocalDescription(await pc.createOffer());
        result.offer_created = true;
        await new Promise(resolve => setTimeout(resolve, 5500));
        result.gathering_state = pc.iceGatheringState;
      } catch (error) {result.error = error.name + ': ' + error.message;}
      finally {if (pc) pc.close();}
      return result;
    });
    await Promise.allSettled([...pending]);
    // Use the site's own IP privacy toggle for the screenshot, leaving metrics unchanged.
    const toggle = page.locator('.ip-mask-toggle input[type=checkbox]');
    if (await toggle.count()) await toggle.check({force: true});
    await page.screenshot({path: path.join(output, 'page.png'), fullPage: true});
    report.title = await page.title();
    report.completed = true;
    console.log(JSON.stringify({browser: report.browser_version, output, site: report.site,
      device: report.device, rtc: report.rtc}, null, 2));
  } catch (error) {
    // Launch logs can include proxy options: scrub the ephemeral session credential.
    let message = error.stack || String(error);
    for (const secret of [proxy.href, proxy.password, decodeURIComponent(proxy.password)])
      if (secret) message = message.split(secret).join('[redacted]');
    report.error = message;
    console.error(message);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  }
}
main().catch(error => {console.error(error.message); process.exitCode = 1;});
