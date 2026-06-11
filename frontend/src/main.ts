import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import './style.css'

const app = createApp(App)
app.use(createPinia())   // Pinia 必须在 router 之前安装（router guard 用到 store）
app.use(router)
app.mount('#app')
