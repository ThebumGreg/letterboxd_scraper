'use strict';
/* A playback clock that runs in the browser.

   The server only learns a position every few seconds (and Jellyfin's apps report it even less often), so drawing each reported value makes the
   timer and bar jump. Instead each playing item gets an "anchor" (a position and the moment it was known) and the page counts forward from it a few
   times a second. Every update from the server corrects the anchor:
     - a small gap is closed gradually, so nothing visibly jumps (and the timer never steps backwards for a gap of a second or so);
     - a big gap (a seek, or a different part of the title) snaps;
     - a pause, resume or seek made on this page takes effect at once and is held for a few seconds, because the server still reports the old
       state until the TV's next report; the server's word is taken as soon as it agrees, or when the hold runs out.
   Times are in seconds. Every function takes the current time as its last argument only so that it can be tested. */
const Clock = (() => {
  const SNAP = 4;          // a gap bigger than this many seconds is a seek: jump to it
  const EASE = 0.35;       // a smaller gap is closed by this fraction at each update
  const BEHIND = 1.5;      // a server position this many seconds or fewer behind ours is just a late report: the timer does not step back for it
  const HOLD = 6;          // seconds a local pause/resume/seek waits for the server to agree
  const anchors = {};
  const now = () => performance.now() / 1000;
  const clamp = (pos, len) => Math.max(0, len > 0 ? Math.min(pos, len) : pos);

  function position(key, t = now()) {
    const a = anchors[key];
    if (!a) return 0;
    return a.playing ? clamp(a.pos + (t - a.at), a.len) : clamp(a.pos, a.len);
  }

  /** What the server says now. -> {pos, playing}: what to show (the server's word, unless a local change is still waiting for it). */
  function sync(key, pos, playing, len, t = now()) {
    const a = anchors[key];
    if (!a) {
      anchors[key] = {pos: clamp(pos, len), at: t, playing, len, holdUntil: 0};
      return {pos: clamp(pos, len), playing};
    }
    a.len = len;
    const here = position(key, t);
    if (t < a.holdUntil) {                                    // a local pause/resume/seek is waiting for the server
      const agrees = playing === a.playing && Math.abs(pos - here) <= SNAP;
      if (!agrees) return {pos: here, playing: a.playing};    // still the old report: keep what the person did
      a.holdUntil = 0;
    }
    if (a.playing !== playing || Math.abs(pos - here) > SNAP) {
      Object.assign(a, {pos: clamp(pos, len), at: t, playing});
    } else if (pos < here && here - pos <= BEHIND) {
      Object.assign(a, {pos: here, at: t});
    } else {
      Object.assign(a, {pos: here + (pos - here) * EASE, at: t});
    }
    return {pos: position(key, t), playing: a.playing};
  }

  /** Pause or resume at once (to: true/false, or leave it out to flip). */
  function setPlaying(key, to, t = now()) {
    const a = anchors[key];
    if (!a) return;
    const next = to === undefined ? !a.playing : to;
    Object.assign(a, {pos: position(key, t), at: t, playing: next, holdUntil: t + HOLD});
  }

  /** Move by this many seconds at once (the ±30 s buttons). */
  function shift(key, seconds, t = now()) {
    const a = anchors[key];
    if (!a) return;
    Object.assign(a, {pos: clamp(position(key, t) + seconds, a.len), at: t, holdUntil: t + HOLD});
  }

  /** Go to this position at once (the seek box). */
  function seekTo(key, pos, t = now()) {
    const a = anchors[key];
    if (!a) return;
    Object.assign(a, {pos: clamp(pos, a.len), at: t, holdUntil: t + HOLD});
  }

  const playing = (key) => !!anchors[key]?.playing;
  return {position, sync, setPlaying, shift, seekTo, playing, SNAP, EASE, BEHIND, HOLD};
})();
if (typeof module !== 'undefined') module.exports = Clock;   // for the tests
