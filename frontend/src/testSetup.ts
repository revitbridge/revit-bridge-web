// Fixtures default to English on every operating system. Language tests can override these.
if (typeof navigator !== 'undefined') {
  Object.defineProperty(navigator, 'languages', { configurable: true, value: ['en-US'] })
  Object.defineProperty(navigator, 'language', { configurable: true, value: 'en-US' })
}
