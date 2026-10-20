"""Two isolated browsers, real WebRTC with fake microphones, mocked VK API."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright, expect
from waitress import create_server

from app import create_app

MOCK_VK = """
window.VK = {VideoPlayer: function() {
  let position = 0, playing = false, updated = performance.now();
  const current = () => position + (playing ? (performance.now() - updated) / 1000 : 0);
  window.mockPlayer = {
    getCurrentTime: current,
    seek: value => { position = value; updated = performance.now(); },
    play: () => { position = current(); updated = performance.now(); playing = true; },
    pause: () => { position = current(); updated = performance.now(); playing = false; },
    getState: () => playing ? 'playing' : 'paused',
    on: (event, fn) => { if (event === 'inited') setTimeout(fn, 50); },
    destroy: () => {}
  };
  return window.mockPlayer;
}};
"""
INSTRUMENT = """
window.testPeers = [];
const Native = window.RTCPeerConnection;
window.RTCPeerConnection = class extends Native {
  constructor(config) { super(config); window.testPeers.push(this); }
};
"""


def main():
    with tempfile.TemporaryDirectory() as temp:
        app = create_app({
            "DATABASE": str(Path(temp) / "browser.sqlite3"),
            "SECRET_KEY": "browser-test",
            "ICE_SERVERS": [],
        })
        connections = {}
        server = create_server(app, host="127.0.0.1", port=8011, threads=12, map=connections)
        stopping = threading.Event()

        def serve():
            while not stopping.is_set():
                server.asyncore.loop(timeout=0.1, count=1, map=connections)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                browser = pw.chromium.launch(
                    executable_path=str(chrome) if chrome.exists() else None,
                    headless=True,
                    args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
                )
                contexts = [
                    browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1000})
                    for _ in range(2)
                ]
                errors = []
                for context in contexts:
                    context.add_init_script(INSTRUMENT)
                    context.route("https://vk.ru/js/api/videoplayer.js", lambda route: route.fulfill(content_type="text/javascript", body=MOCK_VK))
                    context.route("https://vk.ru/video_ext.php?**", lambda route: route.fulfill(content_type="text/html", body="<body style='background:#101116;color:#b6ef72'>VK test player</body>"))
                    context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
                owner, guest = [context.new_page() for context in contexts]
                for page in (owner, guest):
                    page.on("pageerror", lambda error: errors.append(str(error)))
                owner.goto("http://127.0.0.1:8011")
                owner.screenshot(path="/tmp/mrc-rooms-home.png", full_page=True)
                owner.locator("#name").fill("Аня")
                owner.locator("#title").fill("Вечер пятницы")
                owner.locator("#url").fill("https://vkvideo.ru/video-123_456")
                owner.locator("#create-form button").click()
                owner.wait_for_url("**/r/**")
                owner.locator("#join-form button").click()
                expect(owner.locator("#join-dialog")).not_to_be_visible()
                guest.goto(owner.url)
                guest.locator("#join-name").fill("Боря")
                guest.locator("#join-form button").click()
                expect(guest.locator("#join-dialog")).not_to_be_visible()
                expect(owner.locator("#member-count")).to_have_text("2 / 8", timeout=10000)
                assert guest.locator("[data-action]").count() == 0
                owner.locator("#chat-input").fill("Привет, <script>alert(1)</script>!")
                owner.locator("#chat-input").press("Enter")
                expect(guest.locator(".message-body")).to_have_text("Привет, <script>alert(1)</script>!", timeout=10000)
                guest.locator("#chat-input").fill("Я здесь")
                guest.locator("#chat-input").press("Enter")
                expect(owner.locator(".message-body").last).to_have_text("Я здесь", timeout=10000)
                owner.wait_for_function("window.mockPlayer !== undefined")
                guest.wait_for_function("window.mockPlayer !== undefined")
                owner.locator("#seek").fill("42")
                owner.locator("#seek-button").click()
                guest.wait_for_function("Math.abs(mockPlayer.getCurrentTime() - 42) < 1")
                owner.locator('[data-action="play"]').click()
                guest.wait_for_function("mockPlayer.getState() === 'playing'")
                owner.locator('[data-action="pause"]').click()
                guest.wait_for_function("mockPlayer.getState() === 'paused'")
                print("PASS: create, invite, presence, bidirectional chat, safe text rendering, VK playback synchronization")

                owner.locator("#voice-join").click()
                guest.locator("#voice-join").click()
                connected = "testPeers.some(peer => peer.connectionState === 'connected')"
                owner.wait_for_function(connected, timeout=30000)
                guest.wait_for_function(connected, timeout=30000)
                guest.wait_for_function("""async () => {
                  const peer = testPeers.find(peer => peer.connectionState === 'connected');
                  if (!peer) return false;
                  const stats = await peer.getStats();
                  return [...stats.values()].some(row => row.type === 'inbound-rtp' && row.kind === 'audio' && row.packetsReceived > 0);
                }""", timeout=15000)
                owner.locator("#mute").click()
                expect(owner.locator("#mute")).to_have_text("Включить микрофон")
                assert owner.evaluate("testPeers.find(p => p.connectionState === 'connected').getSenders()[0].track.enabled") is False
                owner.locator("#mute").click()
                expect(owner.locator("#mute")).to_have_text("Выключить микрофон")
                assert owner.evaluate("testPeers.find(p => p.connectionState === 'connected').getSenders()[0].track.enabled") is True
                owner.screenshot(path="/tmp/mrc-rooms-room.png", full_page=True)
                guest.locator("#voice-leave").click()
                guest.wait_for_function("testPeers.every(peer => peer.connectionState === 'closed')")
                owner.wait_for_function("testPeers.every(peer => peer.connectionState === 'closed')", timeout=10000)
                guest.locator("#voice-join").click()
                owner.wait_for_function(connected, timeout=30000)
                guest.wait_for_function(connected, timeout=30000)
                print("PASS: real WebRTC connection, inbound audio packets, mute/unmute, leave, rejoin")

                owner.set_viewport_size({"width": 390, "height": 844})
                owner.screenshot(path="/tmp/mrc-rooms-mobile.png", full_page=True)
                assert owner.evaluate("document.documentElement.scrollWidth <= innerWidth")
                guest.reload()
                guest.locator("#join-form button").click()
                expect(guest.locator(".message-body").last).to_have_text("Я здесь", timeout=10000)
                assert not errors, errors
                print("PASS: mobile layout, history after reload, no uncaught JavaScript errors")
                browser.close()
        finally:
            # Stop the event loop before closing its descriptors.
            stopping.set()
            server.pull_trigger()
            thread.join(timeout=5)
            server.task_dispatcher.shutdown()
            for connection in list(connections.values()):
                connection.close()


SOAK_INSTRUMENT = INSTRUMENT + """
// Keep a reference to the real VK API without replacing its behavior.
let vk;
Object.defineProperty(window, 'VK', {
  configurable: true,
  get: () => vk,
  set: value => {
    vk = value;
    let constructor;
    Object.defineProperty(vk, 'VideoPlayer', {
      configurable: true,
      get: () => constructor,
      set: original => {
        constructor = function(...args) {
          const player = original(...args);
          window.testVideo = player;
          return player;
        };
        Object.assign(constructor, original);
      }
    });
  }
});
"""

SOAK_SAMPLE = """async () => {
  const peers = window.testPeers.filter(p => p.connectionState !== 'closed');
  const stats = await Promise.all(peers.map(p => p.getStats()));
  const inbound = stats.map(set => [...set.values()].filter(
    row => row.type === 'inbound-rtp' && row.kind === 'audio'
  ).reduce((total, row) => total + (row.packetsReceived || 0), 0));
  const player = window.mockPlayer || window.testVideo;
  return {
    members: document.querySelector('#member-count').textContent,
    connected: peers.filter(p => p.connectionState === 'connected').length,
    inbound,
    mediaState: player?.getState(),
    mediaError: player?.getErrorCode?.(),
    position: player?.getCurrentTime(),
    online: document.querySelector('#connection').textContent,
    messages: document.querySelectorAll('.chat-message').length,
    mic: peers.some(p => p.getSenders().some(s => s.track?.enabled)),
  };
}"""


async def soak(base_url, seconds, video_url, report_path):
    """Eight independent sessions over the supplied URL, including real audio."""
    report = {
        "base_url": base_url,
        "requested_seconds": seconds,
        "users": 8,
        "video": "real VK" if video_url else "mock VK (not a film)",
        "samples": [],
        "http_errors": [],
        "network_errors": [],
        "js_errors": [],
        "assertions_failed": [],
        "passed": False,
    }
    durations = []
    active = True

    async def sample_client(page):
        result = await page.evaluate(SOAK_SAMPLE)
        if video_url:
            handle = await page.locator("#vk-player").element_handle()
            frame = await handle.content_frame()
            result["decoded"] = await frame.locator("video.player-media").first.evaluate("""v => ({
              time: v.currentTime, paused: v.paused, ready: v.readyState,
              frames: v.getVideoPlaybackQuality().totalVideoFrames,
              dropped: v.getVideoPlaybackQuality().droppedVideoFrames,
              error: v.error?.message || null
            })""")
        return result

    def response_received(response):
        if active and response.url.startswith(base_url + "/api/"):
            if response.status >= 400:
                report["http_errors"].append({"status": response.status, "url": response.url.split("?")[0]})

    def request_finished(request):
        if active and request.url.startswith(base_url + "/api/"):
            timing = request.timing
            if timing["responseEnd"] > 0:
                durations.append(timing["responseEnd"])

    def request_failed(request):
        if active and request.url.startswith(base_url + "/api/"):
            report["network_errors"].append({"url": request.url.split("?")[0], "error": request.failure})

    async with async_playwright() as pw:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        browser = await pw.chromium.launch(
            executable_path=str(chrome) if chrome.exists() else None,
            headless=True,
            args=[
                "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ],
        )
        contexts = []
        pages = []
        try:
            for _ in range(8):
                context = await browser.new_context(
                    permissions=["microphone"], viewport={"width": 1280, "height": 900},
                )
                instrument = SOAK_INSTRUMENT if video_url else INSTRUMENT
                await context.add_init_script("if (window.top === window) {\n" + instrument + "\n}")
                if not video_url:
                    await context.route(
                        "https://vk.ru/js/api/videoplayer.js",
                        lambda route: route.fulfill(content_type="text/javascript", body=MOCK_VK),
                    )
                    await context.route(
                        "https://vk.ru/video_ext.php?**",
                        lambda route: route.fulfill(content_type="text/html", body="<p>Тест синхронизации · VK mock</p>"),
                    )
                contexts.append(context)
                page = await context.new_page()
                page.on("response", response_received)
                page.on("requestfinished", request_finished)
                page.on("requestfailed", request_failed)
                page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
                pages.append(page)

            owner = pages[0]
            await owner.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            # Free localhost.run shows an initial visitor confirmation page.
            if await owner.locator("#create-form").count() == 0:
                confirm = owner.get_by_role("button", name="Continue", exact=False)
                if await confirm.count():
                    await confirm.first.click()
            await owner.locator("#name").fill("Тест 1")
            await owner.locator("#title").fill("Проверка: 8 участников · 10 минут")
            await owner.locator("#url").fill(video_url or "https://vkvideo.ru/video-123_456")
            await owner.locator("#create-form button").click()
            await owner.wait_for_url("**/r/**", timeout=30000)
            report["room_url"] = owner.url
            print(f"ROOM: {owner.url}", flush=True)

            async def join(page, index):
                if index:
                    await page.goto(owner.url, wait_until="domcontentloaded", timeout=60000)
                await page.locator("#join-name").fill(f"Тест {index + 1}")
                await page.locator("#join-form button").click()
                await page.locator("#join-dialog").wait_for(state="hidden", timeout=30000)
                await page.locator("#voice-join").click()
                await page.locator("#mute").wait_for(state="visible", timeout=30000)

            await asyncio.gather(*(join(page, index) for index, page in enumerate(pages)))
            print("Eight participants joined; negotiating 28 audio connections…", flush=True)
            if video_url:
                async def activate_video(page):
                    await page.frame_locator("#vk-player").locator("video").first.wait_for(state="attached", timeout=60000)
                    # VK initializes its player after a visitor clicks the poster.
                    await page.locator("#vk-player").click()
                await asyncio.gather(*(activate_video(page) for page in pages))
            for page in pages:
                await page.wait_for_function(
                    "testPeers.filter(p => p.connectionState === 'connected').length === 7",
                    timeout=90000,
                )
                await page.wait_for_function(
                    "(window.mockPlayer || window.testVideo)?.getState() !== 'uninited' && !!(window.mockPlayer || window.testVideo)",
                    timeout=60000,
                )
            await owner.locator('[data-action="play"]').click()
            for page in pages:
                await page.wait_for_function(
                    "(window.mockPlayer || window.testVideo)?.getState() === 'playing'",
                    timeout=60000,
                )
                if video_url:
                    handle = await page.locator("#vk-player").element_handle()
                    frame = await handle.content_frame()
                    # VK's media element is inside an open shadow root.
                    video = await frame.locator("video.player-media").first.element_handle()
                    await frame.wait_for_function("""video => {
                      return video && !video.paused && video.readyState >= 3
                        && video.getVideoPlaybackQuality().totalVideoFrames > 0;
                    }""", arg=video, timeout=60000)
            print(f"START: {seconds}s, 8 browsers, 28 real WebRTC connections, {report['video']}", flush=True)
            started = time.monotonic()
            last_chat = -1
            previous_packets = None
            previous_frames = None
            while True:
                elapsed = time.monotonic() - started
                minute = int(elapsed // 60)
                if minute != last_chat and elapsed < seconds:
                    last_chat = minute

                    async def chat(page, index):
                        await page.locator("#chat-input").fill(f"Участник {index + 1}: минута {minute + 1}")
                        await page.locator("#chat-input").press("Enter")

                    await asyncio.gather(*(chat(page, index) for index, page in enumerate(pages)))
                samples = await asyncio.gather(*(sample_client(page) for page in pages))
                positions = [sample["position"] for sample in samples if isinstance(sample.get("position"), (int, float))]
                drift = max(positions) - min(positions) if len(positions) == 8 else None
                sample = {"elapsed": round(elapsed, 1), "drift": round(drift, 3) if drift is not None else None, "clients": samples}
                report["samples"].append(sample)
                issues = []
                for index, client in enumerate(samples):
                    if client["members"] != "8 / 8" or client["connected"] != 7:
                        issues.append(f"user {index + 1}: membership/connections {client['members']}/{client['connected']}")
                    if client["mediaState"] != "playing" or client["online"] != "В комнате" or not client["mic"]:
                        issues.append(f"user {index + 1}: playback/presence/mic problem")
                    if len(client["inbound"]) != 7 or any(packets == 0 for packets in client["inbound"]):
                        issues.append(f"user {index + 1}: missing inbound audio")
                    if previous_packets is not None and sum(client["inbound"]) <= previous_packets[index]:
                        issues.append(f"user {index + 1}: inbound audio stopped")
                    if video_url:
                        decoded = client["decoded"]
                        if decoded["error"] or decoded["paused"] or decoded["ready"] < 2:
                            issues.append(f"user {index + 1}: real video not playing: {decoded}")
                        if previous_frames is not None and decoded["frames"] <= previous_frames[index]:
                            issues.append(f"user {index + 1}: no newly decoded video frames")
                previous_packets = [sum(client["inbound"]) for client in samples]
                if video_url:
                    previous_frames = [client["decoded"]["frames"] for client in samples]
                if drift is None or drift > 3:
                    issues.append(f"video drift exceeds 3 seconds: {drift}")
                report["assertions_failed"].extend({"elapsed": round(elapsed, 1), "issue": issue} for issue in issues)
                connections = sum(client["connected"] for client in samples) // 2
                print(f"{elapsed:6.1f}s | users=8 | audio connections={connections}/28 | drift={drift:.3f}s | errors={len(issues)}", flush=True)
                if elapsed >= seconds:
                    break
                await asyncio.sleep(min(15, seconds - elapsed))

            await owner.locator('[data-action="pause"]').click()
            await asyncio.gather(*(page.wait_for_function(
                "(window.mockPlayer || window.testVideo)?.getState() === 'paused'", timeout=20000
            ) for page in pages))
            # Confirm all eight clients received the final minute's chat messages.
            marker = f"Участник 8: минута {last_chat + 1}"
            for page in pages:
                await page.get_by_text(marker, exact=True).wait_for(timeout=15000)
            await owner.screenshot(path=str(Path(report_path).with_suffix(".png")), full_page=True)
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            report["passed"] = not any(report[key] for key in ("http_errors", "network_errors", "js_errors", "assertions_failed"))
        except Exception as error:
            report["fatal_error"] = str(error)
            report["diagnostics"] = []
            for index, page in enumerate(pages):
                try:
                    state = await page.evaluate(SOAK_SAMPLE)
                    state["sync_status"] = await page.locator("#sync-status").inner_text()
                    report["diagnostics"].append(state)
                    if index == 0:
                        await page.screenshot(path=str(Path(report_path).with_suffix(".png")), full_page=True)
                except Exception:
                    pass
            raise
        finally:
            active = False
            if durations:
                ordered = sorted(durations)
                report["api_requests"] = len(ordered)
                report["api_p50_ms"] = round(ordered[len(ordered) // 2], 1)
                report["api_p95_ms"] = round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 1)
                report["api_max_ms"] = round(ordered[-1], 1)
            Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(f"REPORT: {report_path} | passed={report['passed']} | requests={len(durations)}", flush=True)
            await browser.close()
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--soak-seconds", type=int, default=0)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--video-url")
    parser.add_argument("--report", default="/tmp/mrc-rooms-soak-report.json")
    args = parser.parse_args()
    if args.soak_seconds:
        asyncio.run(soak(args.base_url.rstrip("/"), args.soak_seconds, args.video_url, args.report))
    else:
        main()
