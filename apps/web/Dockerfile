FROM node:24-slim
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci
COPY apps/web apps/web
RUN npm run build
CMD ["npm", "run", "start", "--workspace", "apps/web", "--", "--hostname", "0.0.0.0"]
