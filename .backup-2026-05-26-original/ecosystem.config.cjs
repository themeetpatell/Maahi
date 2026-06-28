module.exports = {
  apps: [
    {
      name: "maahi",
      script: "server/index.js",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "500M",
      env_production: {
        NODE_ENV: "production",
      },
    },
  ],
};
