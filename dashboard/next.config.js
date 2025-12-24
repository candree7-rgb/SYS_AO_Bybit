/** @type {import('next').NextConfig} */
const nextConfig = {
  // Removed 'output: standalone' - not compatible with 'next start'
  experimental: {
    serverActions: {
      bodySizeLimit: '2mb',
    },
  },
}

module.exports = nextConfig
