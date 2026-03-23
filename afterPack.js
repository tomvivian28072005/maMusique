const path = require('path');
const { rcedit } = require('rcedit');

exports.default = async function (context) {
  const exePath = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.exe`);
  const iconPath = path.join(context.packager.projectDir, 'logo.ico');

  console.log(`[afterPack] Injection icône dans ${exePath}`);
  await rcedit(exePath, { icon: iconPath });
  console.log('[afterPack] Icône injectée');
};
