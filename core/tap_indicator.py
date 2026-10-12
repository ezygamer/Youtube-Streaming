"""Shows where the bot taps: a ring pops at every click inside the game.

The bot clicks through Playwright, so there is no visible mouse cursor -
viewers (and you, watching the window) would only see cells change. This
script is injected at runtime into the game's page (the game files are not
modified); it draws a short ring animation at each pointer press. It is
purely visual and ignores pointer events, so it can't affect the game.
"""

TAP_INDICATOR_JS = r"""
(() => {
  // Only inside the game, not in the stream overlay page itself.
  if (location.pathname.startsWith('/__stream__')) return;
  const install = () => {
    const style = document.createElement('style');
    style.textContent = `
      .ez-tap { position: fixed; z-index: 2147483647; pointer-events: none;
        width: 64px; height: 64px; margin: -32px 0 0 -32px; border-radius: 50%;
        border: 6px solid rgba(255, 140, 0, 0.95); box-shadow: 0 0 18px rgba(255, 140, 0, 0.8);
        animation: ez-tap 0.6s ease-out forwards; }
      .ez-tap::after { content: ''; position: absolute; inset: 18px; border-radius: 50%;
        background: rgba(255, 140, 0, 0.9); }
      @keyframes ez-tap { from { transform: scale(0.4); opacity: 1; } to { transform: scale(1.3); opacity: 0; } }`;
    document.head.appendChild(style);
    document.addEventListener('pointerdown', (e) => {
      const ring = document.createElement('div');
      ring.className = 'ez-tap';
      ring.style.left = e.clientX + 'px';
      ring.style.top = e.clientY + 'px';
      document.body.appendChild(ring);
      setTimeout(() => ring.remove(), 650);
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
"""
