(function() {
  'use strict';

  var bgScene = document.querySelector('.bg-scene');
  if (!bgScene) return;

  var isMobile = window.matchMedia && window.matchMedia('(max-width: 640px)').matches;
  if (Math.random() < 0.5) {
    bgScene.classList.add(isMobile ? 'bg-mobile-alt' : 'bg-pc-macbook');
  }
})();
