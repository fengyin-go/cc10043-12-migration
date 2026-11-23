import { createRouter, createWebHistory } from 'vue-router'

import MigrationConsole from '@/views/MigrationConsole.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/migration-console', name: 'migration-console', component: MigrationConsole },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
