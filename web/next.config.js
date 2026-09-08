/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  experimental: {
    typedRoutes: true,
  },
  async rewrites() {
    // Внутри docker-сети backend доступен как http://backend:8000.
    // http://localhost:8000 здесь означал бы сам контейнер web → ECONNREFUSED.
    // Для локальной разработки (npm run dev вне docker) переопределите через env:
    //   KUMAFLOW_API_URL=http://localhost:8000
    const api = process.env.KUMAFLOW_API_URL ?? "http://backend:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${api}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
