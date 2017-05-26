/*
 * Call quitter to exit the app after the page load event fires.
 *
 * Android 4.0, 4.2 call the load event handler but do not
 * cause the app to quit, therefore we have added an additional
 * call to quit which will be executed after 5 seconds. This
 * should be long enough period for the page to complete loading
 * if it is going to do so.
 */
function quit()
{
  try {
    if (typeof(AutophoneQuitter) != "undefined") {
      AutophoneQuitter.quit();
    } else if (typeof(Quitter) != "undefined") {
      Quitter.quit();
    } else {
      dump('quit: No Quitter available!');
    }
  } catch(ex) {
    dump('quit: Exception ' + ex);
  }
}

document.addEventListener("load", function () { setTimeout(quit, 1000); }, false);
setTimeout(quit, 5000);
