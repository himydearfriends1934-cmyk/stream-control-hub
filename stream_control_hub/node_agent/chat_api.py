"""Chat-plan API routes for the VPS stream node agent."""

from __future__ import annotations

from flask import jsonify, request

from .chat import load_chat_plan, save_chat_plan_data
from .runtime import APP, protected

@APP.route("/api/chat-plan", methods=["GET", "POST"])
@protected
def api_chat_plan():
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "chat_plan": load_chat_plan(),
        })

    payload = request.get_json(silent=True) or {}
    messages = [
        line.strip()
        for line in str(payload.get("chatMessagesInput", "")).splitlines()
        if line.strip()
    ]
    plan = save_chat_plan_data({
        "enabled": load_chat_plan().get("enabled", False),
        "interval_seconds": payload.get("chatIntervalInput", 300),
        "mode": payload.get("chatModeInput", "loop"),
        "messages": messages,
    })
    return jsonify({
        "ok": True,
        "message": "聊天配置已保存到服务器",
        "chat_plan": plan,
    })


@APP.route("/api/chat-plan/toggle", methods=["POST"])
@protected
def api_chat_plan_toggle():
    payload = request.get_json(silent=True) or {}
    current = load_chat_plan()
    current["enabled"] = bool(payload.get("enabled", False))
    plan = save_chat_plan_data(current)
    return jsonify({
        "ok": True,
        "message": "聊天计划状态已更新",
        "chat_plan": plan,
    })


@APP.route("/chat-helper.js")
@protected
def chat_helper_js():
    origin = request.host_url.rstrip("/")
    script = f"""
(() => {{
  if (window.__ytLiveChatHelperLoaded) {{
    console.log('YouTube 聊天助手已经在运行。');
    return;
  }}
  window.__ytLiveChatHelperLoaded = true;
  const ORIGIN = {origin!r};
  let lastSentAt = 0;
  let lastIndex = -1;

  function createBadge() {{
    const box = document.createElement('div');
    box.id = 'yt-live-chat-helper-badge';
    box.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:999999;background:#111827;color:#f8fafc;padding:10px 14px;border-radius:14px;border:1px solid rgba(148,163,184,.2);font:12px/1.6 Segoe UI,Microsoft YaHei,sans-serif;box-shadow:0 12px 28px rgba(0,0,0,.35);max-width:320px;';
    box.textContent = 'YouTube 聊天助手已启动，正在等待聊天计划...';
    document.body.appendChild(box);
    return box;
  }}

  const badge = createBadge();

  function updateBadge(text) {{
    badge.textContent = text;
  }}

  function getDocuments(root = window.document) {{
    const docs = [root];
    for (const iframe of root.querySelectorAll('iframe')) {{
      try {{
        if (iframe.contentDocument) docs.push(...getDocuments(iframe.contentDocument));
      }} catch (e) {{}}
    }}
    return docs;
  }}

  function findChatInputAndButton() {{
    const docs = getDocuments();
    for (const doc of docs) {{
      const input = doc.querySelector('yt-live-chat-text-input-field-renderer #input[contenteditable=\"true\"]')
        || doc.querySelector('#input[contenteditable=\"true\"]')
        || doc.querySelector('[contenteditable=\"true\"][aria-label]');
      const button = doc.querySelector('yt-button-renderer#send-button button')
        || doc.querySelector('#send-button button')
        || doc.querySelector('button[aria-label*=\"Send\"]')
        || doc.querySelector('button[aria-label*=\"发送\"]');
      if (input && button) {{
        return {{ input, button }};
      }}
    }}
    return null;
  }}

  function setInputValue(input, text) {{
    input.focus();
    input.textContent = '';
    document.execCommand && document.execCommand('insertText', false, text);
    if (!input.textContent || input.textContent.trim() !== text.trim()) {{
      input.textContent = text;
    }}
    input.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: text }}));
  }}

  function pickMessage(plan) {{
    const messages = plan.messages || [];
    if (!messages.length) return null;
    if (plan.mode === 'random') {{
      return messages[Math.floor(Math.random() * messages.length)];
    }}
    lastIndex = (lastIndex + 1) % messages.length;
    return messages[lastIndex];
  }}

  async function fetchPlan() {{
    const resp = await fetch(`${{ORIGIN}}/api/chat-plan`, {{ cache: 'no-store' }});
    return resp.json();
  }}

  async function tick() {{
    try {{
      const data = await fetchPlan();
      const plan = data.chat_plan || {{}};
      if (!plan.enabled) {{
        updateBadge('聊天计划已暂停，等待你在面板里启用。');
        return;
      }}
      if (!(plan.messages || []).length) {{
        updateBadge('聊天计划已启用，但还没有消息内容。');
        return;
      }}

      const now = Date.now();
      const intervalMs = Math.max(10, Number(plan.interval_seconds || 300)) * 1000;
      const waitLeft = Math.max(0, intervalMs - (now - lastSentAt));
      const target = findChatInputAndButton();
      if (!target) {{
        updateBadge('没找到 YouTube 聊天输入框。请保持聊天页面打开，并确保账号已登录。');
        return;
      }}

      if (lastSentAt && waitLeft > 0) {{
        updateBadge(`聊天计划运行中，${{Math.ceil(waitLeft / 1000)}} 秒后发送下一条。`);
        return;
      }}

      const message = pickMessage(plan);
      if (!message) {{
        updateBadge('没有可发送的消息。');
        return;
      }}

      setInputValue(target.input, message);
      target.button.click();
      lastSentAt = now;
      updateBadge(`已发送：${{message}}`);
    }} catch (err) {{
      updateBadge(`聊天助手报错：${{err.message}}`);
    }}
  }}

  updateBadge('YouTube 聊天助手已启动，正在读取服务器里的聊天计划...');
  tick();
  setInterval(tick, 3000);
}})();
"""
    return APP.response_class(script, mimetype="application/javascript")
