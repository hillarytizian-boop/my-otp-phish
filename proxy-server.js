const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');

const app = express();
app.use(cors());
app.use(express.json());

app.all('*', async (req, res) => {
  try {
    const targetUrl = `https://api.binance.com${req.url}`;
    const response = await fetch(targetUrl, {
      method: req.method,
      headers: { ...req.headers, host: 'api.binance.com' },
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.listen(process.env.PORT || 3000, () => {
  console.log(`Proxy running on port ${process.env.PORT || 3000}`);
});
