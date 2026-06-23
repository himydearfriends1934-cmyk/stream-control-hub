"""Dashboard HTML templates for the stream node agent."""

LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Istanbul Stream Node登录</title>
  <style>
    :root {
      --bg: #050816;
      --panel: rgba(10, 18, 42, 0.92);
      --line: rgba(103, 232, 249, 0.18);
      --text: #edfaff;
      --muted: #8faac0;
      --accent: #fb923c;
      --accent2: #a855f7;
      --bad: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 20px;
      color: var(--text);
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(34,211,238,0.22), transparent 28%),
        radial-gradient(circle at left bottom, rgba(168,85,247,0.2), transparent 32%),
        linear-gradient(145deg, rgba(30,41,79,0.72), transparent 52%),
        var(--bg);
    }
    .panel {
      width: min(100%, 460px);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 26px;
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.48);
      backdrop-filter: blur(10px);
      padding: 28px;
    }
    h1 { margin: 0 0 10px; font-size: 32px; }
    p { margin: 0 0 22px; color: var(--muted); line-height: 1.7; }
    label { display: block; margin-bottom: 8px; color: var(--muted); font-size: 14px; }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(5, 10, 28, 0.8);
      color: var(--text);
      padding: 14px 16px;
      font-size: 16px;
      outline: none;
      margin-bottom: 14px;
    }
    input:focus {
      border-color: rgba(20,184,166,0.45);
      box-shadow: 0 0 0 3px rgba(20,184,166,0.12);
    }
    button {
      width: 100%;
      border: none;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      color: #effcf9;
      font-weight: 700;
      font-size: 16px;
      padding: 14px 18px;
      cursor: pointer;
    }
    .error {
      margin-bottom: 14px;
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(248,113,113,0.12);
      color: #fecaca;
      border: 1px solid rgba(248,113,113,0.2);
    }
  </style>
</head>
<body>
  <form class="panel" method="post">
    <h1>Istanbul Stream Node</h1>
    <p>Istanbul Edge Edition · 独立直播监控与推流控制台</p>
    __ERROR_HTML__
    <input type="hidden" name="next" value="__NEXT_VALUE__">
    <label for="password">控制台密码</label>
    <input id="password" name="password" type="password" placeholder="请输入密码" autocomplete="current-password" required>
    <button type="submit">进入控制台</button>
  </form>
</body>
</html>
"""

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Istanbul Stream Node登录</title>
  <style>
    :root {
      --bg: #050816;
      --panel: rgba(10, 18, 42, 0.92);
      --line: rgba(103, 232, 249, 0.18);
      --text: #edfaff;
      --muted: #8faac0;
      --accent: #fb923c;
      --accent2: #a855f7;
      --bad: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 20px;
      color: var(--text);
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(34,211,238,0.22), transparent 28%),
        radial-gradient(circle at left bottom, rgba(168,85,247,0.2), transparent 32%),
        linear-gradient(145deg, rgba(30,41,79,0.72), transparent 52%),
        var(--bg);
    }
    .panel {
      width: min(100%, 460px);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 26px;
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.48);
      backdrop-filter: blur(10px);
      padding: 28px;
    }
    h1 { margin: 0 0 10px; font-size: 32px; }
    p { margin: 0 0 22px; color: var(--muted); line-height: 1.7; }
    label { display: block; margin-bottom: 8px; color: var(--muted); font-size: 14px; }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(5, 10, 28, 0.8);
      color: var(--text);
      padding: 14px 16px;
      font-size: 16px;
      outline: none;
      margin-bottom: 14px;
    }
    input:focus {
      border-color: rgba(20,184,166,0.45);
      box-shadow: 0 0 0 3px rgba(20,184,166,0.12);
    }
    button {
      width: 100%;
      border: none;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      color: #effcf9;
      font-weight: 700;
      font-size: 16px;
      padding: 14px 18px;
      cursor: pointer;
    }
    .error {
      margin-bottom: 14px;
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(248,113,113,0.12);
      color: #fecaca;
      border: 1px solid rgba(248,113,113,0.2);
    }
  </style>
</head>
<body>
  <form class="panel" method="post">
    <h1>Istanbul Stream Node</h1>
    <p>Istanbul Edge Edition · 独立直播监控与推流控制台</p>
    __ERROR_HTML__
    <input type="hidden" name="next" value="__NEXT_VALUE__">
    <label for="password">控制台密码</label>
    <input id="password" name="password" type="password" placeholder="请输入密码" autocomplete="current-password" required>
    <button type="submit">进入控制台</button>
  </form>
</body>
</html>
"""

