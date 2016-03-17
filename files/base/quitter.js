function quit()
{
  if (typeof(AutophoneQuitter) != "undefined") {
    AutophoneQuitter.quit();
  } else if (typeof(Quitter) != "undefined") {
    Quitter.quit();
  }
}
window.addEventListener("load", function () { setTimeout(quit, 1000); }, false);
