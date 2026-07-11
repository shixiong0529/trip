(function() {
  'use strict';

  var bgScene = document.querySelector('.bg-scene');
  if (!bgScene) return;

  var isMobile = window.matchMedia && window.matchMedia('(max-width: 640px)').matches;
  if (isMobile) return;

  if (Math.random() < 0.5) {
    bgScene.classList.add('bg-pc-macbook');
  }
})();
