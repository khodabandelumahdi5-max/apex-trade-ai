# AlphaDuel mobile shell (Capacitor)

The phone app is a thin native shell that loads the same web app the server already serves
(`https://app.<your-domain>`). There is one codebase, and a server update reaches every phone at once without a new
store release. `www/index.html` is only the offline screen.

## Build
1. Set `appId` (reverse domain you own, e.g. `com.yourcompany.alphaduel`) and replace `YOUR-DOMAIN` in
   `capacitor.config.json`.
2. Install Node 20+ and run `npm install`.
3. Android: install Android Studio, run `npm run add:android`, then `npm run sync`, then `npm run open:android`.
   Then use Build → Generate Signed Bundle (.aab). Keep the signing key safe: losing it means you can never update
   the app.
4. iOS: you need a Mac with Xcode. Run `npm run add:ios`, then `npm run sync`, then `npm run open:ios`, then
   Product → Archive.

## Store review: read before submitting
- **Payments.** Apple (3.1.1) and Google Play (Payments policy) require their own in-app purchase for digital
  subscriptions. A USDT payment screen inside the app will be rejected. Possible routes:
  - free access in the store build;
  - selling the subscription through the store's billing (StoreKit / Play Billing; the server would then verify
    store receipts);
  - only where the rules allow it, a link to an external payment.
  Check the current rules for every country you publish in.
- **Contests (Apple 5.3).** Official rules must be in the app. The store must not be named as a sponsor. Prizes
  must not depend on paid entry.
- **Minimum functionality (Apple 4.2).** Pure website wrappers get rejected. The app needs app-like value: native
  navigation, push notifications for stops and disqualification (`@capacitor/push-notifications`), and offline
  handling.
- **Account deletion** is already in the app: Account → Delete account. Store listings also need a privacy policy
  URL (`/privacy`) and the data-safety / privacy-label forms.
- **Developer account.** It must belong to a real person or company that is legally allowed to hold it and that
  can receive the store's payouts. Opening one in someone else's name to get around sanctions or country
  restrictions breaks the store terms, can break the law, and can get the account and the app removed.
