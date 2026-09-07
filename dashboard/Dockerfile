# 调用监控面板：纯静态前端，用 nginx:alpine 托管。
# 构建上下文为 ./dashboard（docker-compose 的 build.context）。
FROM nginx:alpine

# 关闭默认 access 日志噪音，错误日志保留
COPY . /usr/share/nginx/html

# 面板为单页应用，/ 即 index.html；资源走相对路径，无需额外路由配置。
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
