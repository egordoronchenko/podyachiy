/* Запуск страницы администрирования — последним сценарием, когда все разделы уже загружены. */
restore().then(() => { loadSidebar(); showPane(location.hash.slice(1)); tick(); });
setInterval(tick, 3000);
