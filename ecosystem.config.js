module.exports = {
  apps: [
    {
      name: "lancers-bot",
      script: "main.py",
      args: "--slack --interval=5",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      restart_delay: 10000,
      env: {
        // Flush prints immediately so pm2 logs are live
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      // WebSocket server for the browser extension (127.0.0.1:8765, nginx in front)
      name: "lancers-server",
      script: "run_server.py",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
