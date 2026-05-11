FROM node:20-alpine

WORKDIR /app

COPY package.json ./
COPY index.js ./

# Runs as root inside an isolated container; writes only to its own named volume.
CMD ["node", "index.js"]
